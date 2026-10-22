"""Agent api: observations concerns."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentClaim,
    AgentEvidenceCode,
    InventoryRequest,
    InventoryVerdict,
    RecipeRunObservationsWire,
    RouteState,
    RunState,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.claims import ClaimRequest
from vonk_agent_protocol.telemetry import TelemetryRequest

from ..agent_acceptance import InventoryAcceptance, ObservationAcceptance
from ..agent_jobs import StaleAgentAttempt
from ..auth import AgentIdentity
from ..download_contract import download_responses
from ..inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    InventorySnapshotInput,
)
from ..library_contract import UuidId
from ..logging import log_event
from ..models import (
    STOPPABLE_NOT_RUNNING_RUN_STATES,
    STOPPABLE_RUN_STATES,
    ClusterMapping,
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from ..operator_acceptance import ReceiptPolicy, operator_acceptance
from ..owner_liveness import builds_in_flight, run_has_live_operation
from ..persisted_waits import schedule_wait
from ..presence import PresenceError
from ..recipe_operations import prepare_exact_recipe_run_observation_nodes
from ..source_bundles import SourceBundleError
from ..spark_store_retention import store_retention_request
from ..telemetry import TelemetryRepository, TelemetrySampleInput
from ..untracked_installations import untracked_installations, untracked_runs
from .common import (
    _CANONICAL_UUID,
    _DIGEST,
    _LOGGER,
    _TELEMETRY_FIELDS,
    RANK_UNREADY_GRACE,
    RECIPE_RUN_DISPOSITION_HEADER,
    RECIPE_RUN_GENERATION_HEADER,
    RECIPE_RUN_UNOWNED,
    AgentApiServices,
    _authenticated_identity,
    _log_evidence_dropped,
    _log_evidence_warnings,
    _now,
    _require_services,
    _scope_identity,
    _validated_authenticated_source,
)
from .ended_runs import reopen_ended_runs_still_running


def install_observations_routes(
    agent: APIRouter, services: AgentApiServices | None
) -> None:
    def helper_identity(request: Request) -> AgentIdentity:
        _scope_identity(request)
        required = _require_services(services)
        return _authenticated_identity(request, required)

    @agent.post(
        "/claim",
        response_model=AgentClaim,
        responses={204: {"description": "No work available"}},
    )
    @operator_acceptance(ReceiptPolicy.AGENT_CLAIM)
    def claim(
        request: Request,
        body: ClaimRequest,
        request_id: Annotated[UuidId | None, Header(alias="X-Request-ID")] = None,
    ) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        source = _validated_authenticated_source(request, required, identity)
        _log_evidence_warnings(body, endpoint="claim", node_id=identity.node_id)
        try:
            result = required.operations.claim(
                identity.node_id,
                identity.certificate_serial,
                body.wait_seconds,
                runtime_identity=body.runtime_identity.model_dump(),
                preflight_fingerprint=body.preflight_fingerprint,
                hostname=body.hostname,
                source=source,
                claim_fence=None
                if request_id is None
                else str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"claim:{identity.node_id}:{identity.certificate_serial}:{request_id}",
                    )
                ),
            )
        except StaleAgentAttempt as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        encoded_claim = canonical_message(AgentClaim.model_validate(result))
        return Response(content=encoded_claim, media_type="application/json")

    @agent.post(
        "/inventory",
        response_model=InventoryVerdict,
        responses={
            204: {"description": "The inventory named no installations or runs"}
        },
    )
    @operator_acceptance(ReceiptPolicy.AGENT_INVENTORY)
    def inventory(body: InventoryRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if body.observed_at.tzinfo is None or body.observed_at.utcoffset() is None:
            raise HTTPException(
                status_code=422, detail="inventory time must be timezone-aware"
            )
        observed_at = body.observed_at.astimezone(UTC)
        now = _now(required.clock()).astimezone(UTC)
        if (
            observed_at > now + MAX_INVENTORY_FUTURE_SKEW
            or now - observed_at > timedelta(hours=24)
        ):
            raise HTTPException(
                status_code=422, detail="inventory time is outside the accepted window"
            )
        # The fabric pair is optional evidence: an address outside the
        # configured fabric policy (or a Controller without one) is not used,
        # and never costs the Spark its mandatory capacity report.
        fabric_address = body.fabric_address
        fabric_bandwidth_mbps = body.fabric_bandwidth_mbps
        if fabric_address is not None:
            accepted = required.fabric_policy is not None
            if required.fabric_policy is not None:
                try:
                    required.fabric_policy.validate(fabric_address)
                except PresenceError:
                    accepted = False
            if not accepted:
                fabric_address = None
                fabric_bandwidth_mbps = None
                _log_evidence_dropped(
                    AgentEvidenceCode.INVENTORY_FABRIC_DROPPED,
                    endpoint="inventory",
                    node_id=identity.node_id,
                )
        _log_evidence_warnings(body, endpoint="inventory", node_id=identity.node_id)
        try:
            snapshot = InventoryRepository(
                required.sessions, clock=required.clock
            ).record(
                InventorySnapshotInput(
                    node_id=identity.node_id,
                    observed_at=observed_at,
                    disk_total_bytes=body.disk_total_bytes,
                    disk_free_bytes=body.disk_free_bytes,
                    host_memory_total_bytes=body.host_memory_total_bytes,
                    host_memory_free_bytes=body.host_memory_free_bytes,
                    gpu_memory_total_bytes=body.gpu_memory_total_bytes,
                    gpu_memory_free_bytes=body.gpu_memory_free_bytes,
                    gpu_count=body.gpu_count,
                    gpu_health=body.gpu_health,
                    memory_pool=body.memory_pool,
                    artifact_store_read_only=body.artifact_store_read_only,
                    capabilities=tuple(body.capabilities),
                    fabric_address=fabric_address,
                    fabric_bandwidth_mbps=fabric_bandwidth_mbps,
                    nvidia_driver_version=body.nvidia_driver_version,
                    container_runtime_version=body.container_runtime_version,
                    network_interfaces=(
                        None
                        if body.network_interfaces is None
                        else tuple(body.network_interfaces)
                    ),
                    nas_route_interface=body.nas_route_interface,
                    host_settings=body.host_settings,
                    unused_model_bytes=body.unused_model_bytes,
                    model_objects=body.model_objects,
                ),
                report=body,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        request.state.inventory_acceptance = InventoryAcceptance(
            snapshot.id, snapshot.evidence_digest
        )
        clock_authority = (
            required.host_runtime_authority if body.host_settings is not None else None
        )
        if body.installations is None and body.runs is None and clock_authority is None:
            # An older agent, or one that could not list them: no verdict.
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        with required.sessions() as session:
            verdict = InventoryVerdict(
                store_retention=store_retention_request(session, identity.node_id, now),
                untracked_installations=untracked_installations(
                    session, body.installations or ()
                ),
                # Absent unless the runs were named: an older agent's strict
                # reader never sees the field.
                untracked_runs=None
                if body.runs is None
                else untracked_runs(session, body.runs),
            )
        if clock_authority is not None:
            try:
                verdict = verdict.model_copy(
                    update={
                        "gpu_clock_grant": clock_authority.gpu_clock_grant(
                            identity.node_id
                        )
                    }
                )
            except ValueError:
                _LOGGER.warning(
                    "selected GPU clock intent unavailable for %s", identity.node_id
                )
        return Response(
            content=canonical_message(verdict), media_type="application/json"
        )

    @agent.post("/telemetry", status_code=status.HTTP_204_NO_CONTENT)
    @operator_acceptance(ReceiptPolicy.AGENT_TELEMETRY)
    def telemetry(body: TelemetryRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        for sample in body.samples:
            _log_evidence_warnings(
                sample, endpoint="telemetry", node_id=identity.node_id
            )
        try:
            TelemetryRepository(required.sessions, clock=required.clock).record_batch(
                identity.node_id,
                tuple(
                    TelemetrySampleInput(
                        boot_id=uuid.UUID(sample.boot_id),
                        **{name: getattr(sample, name) for name in _TELEMETRY_FIELDS},
                    )
                    for sample in body.samples
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post("/recipe-runs/observations", status_code=status.HTTP_204_NO_CONTENT)
    @operator_acceptance(ReceiptPolicy.AGENT_OBSERVATIONS)
    def recipe_run_observations(
        body: RecipeRunObservationsWire, request: Request
    ) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        observed_at = body.observed_at.astimezone(UTC)
        now = _now(required.clock()).astimezone(UTC)
        if observed_at > now + timedelta(seconds=30) or now - observed_at > timedelta(
            minutes=5
        ):
            raise HTTPException(
                status_code=422,
                detail="recipe run observation time is outside the accepted window",
            )
        # One stale or no-longer-assigned run must not discard the evidence
        # for every other run in the report: each run is judged on its own.
        rejected: list[str] = []
        accepted = 0
        observed_nodes: list[str] = []
        by_run = {run.run_id: run for run in body.runs}
        try:
            with required.sessions.begin() as session:
                included = set(by_run)
                if included:
                    current = set(
                        session.scalars(
                            select(RunNode.run_id)
                            .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                            .where(
                                RunNode.node_id == identity.node_id,
                                RecipeRun.state.in_(STOPPABLE_RUN_STATES),
                            )
                        )
                    )
                    # An ended run's process that is still running is the
                    # Spark's evidence that reopens the run, never dropped.
                    reopened = reopen_ended_runs_still_running(
                        session,
                        identity.node_id,
                        observed_at,
                        now,
                        {run_id: by_run[run_id] for run_id in included - current},
                    )
                    accepted += len(reopened)
                    if reopened:
                        observed_nodes.extend(
                            session.scalars(
                                select(RunNode.id).where(
                                    RunNode.node_id == identity.node_id,
                                    RunNode.run_id.in_(reopened),
                                )
                            )
                        )
                    rejected.extend(
                        f"recipe run {run_id} observation is not assigned"
                        for run_id in sorted(included - current - reopened)
                    )
                    included &= current
                # Only runs no longer here: the report crossed their stop.
                assigned = (
                    ()
                    if by_run and not included
                    else prepare_exact_recipe_run_observation_nodes(
                        session, identity.node_id, observed_at, included
                    )
                )
                for node in assigned:
                    evidence = by_run.get(node.run_id)
                    if evidence is None or node.run_id not in included:
                        continue
                    run = session.get(RecipeRun, node.run_id)
                    assert run is not None
                    if evidence.run_generation != run.run_generation:
                        rejected.append("recipe run observation generation is stale")
                        continue
                    if run.state in STOPPABLE_NOT_RUNNING_RUN_STATES:
                        # A stoppable run that is not running (a cancelled
                        # start left it lost) can never be advanced by this
                        # report, but the Spark's own current word on its
                        # process is the evidence that releases its claim.
                        if (
                            run_has_live_operation(session, run.id)
                            or _now(node.updated_at).astimezone(UTC) > observed_at
                            or _now(run.updated_at).astimezone(UTC) > observed_at
                        ):
                            continue
                        accepted += 1
                        observed_nodes.append(node.id)
                        node.observed_run_generation = run.run_generation
                        node.observation_process_running = evidence.process_running
                        node.observation_observed_at = observed_at
                        if (
                            not evidence.process_running
                            and evidence.failure_diagnostics
                        ):
                            # The exit's cause outlives the settle it allows.
                            node.last_exit_diagnostics = (
                                evidence.failure_diagnostics.model_dump(
                                    mode="json", exclude_none=True
                                )
                            )
                        continue
                    if node.state not in {RunState.RUNNING, RunState.FAILED} or (
                        node.state == "failed"
                        and run.route_state != RouteState.WITHDRAWN
                    ):
                        # The start or recovery operation owns this rank now.
                        continue
                    if _now(node.updated_at).astimezone(UTC) > observed_at:
                        rejected.append("recipe run observation is stale")
                        continue
                    accepted += 1
                    observed_nodes.append(node.id)
                    # The grace period bounds the first observation of a
                    # generation, not every later one.
                    initial_observation_late = (
                        node.observed_run_generation != run.run_generation
                        and run.observation_deadline_at is not None
                        and observed_at > _now(run.observation_deadline_at)
                    )
                    mapping = session.get(ClusterMapping, run.mapping_id)
                    owner = (
                        mapping is not None
                        and mapping.endpoint_owner_node_id == identity.node_id
                    )
                    if initial_observation_late:
                        # Late first evidence cannot make the run routable, but
                        # its process result remains valid evidence for
                        # Controller-owned recovery.
                        node.state = "failed"
                    else:
                        # Failure is an observation, not a latch: fresh live
                        # evidence can recover a rank still owned by this run.
                        # A running process whose own health probe fails is
                        # failed only once that has lasted the grace period;
                        # one missed probe never takes a workload down.
                        unready = (
                            owner
                            and evidence.process_running
                            and evidence.endpoint_ready is not True
                        )
                        since = (
                            _now(node.observation_unready_since).astimezone(UTC)
                            if unready and node.observation_unready_since is not None
                            else observed_at
                        )
                        node.state = (
                            RunState.RUNNING
                            if evidence.process_running
                            and (
                                not unready or observed_at - since < RANK_UNREADY_GRACE
                            )
                            else RunState.FAILED
                        )
                        if unready and node.state == "failed":
                            log_event(
                                _LOGGER,
                                "recipe.rank.unhealthy",
                                service="control-api",
                                run_id=run.id,
                                node_id=identity.node_id,
                                reason="endpoint readiness probe failing",
                                unready_seconds=int(
                                    (observed_at - since).total_seconds()
                                ),
                            )
                        elif not evidence.process_running:
                            log_event(
                                _LOGGER,
                                "recipe.rank.unhealthy",
                                service="control-api",
                                run_id=run.id,
                                node_id=identity.node_id,
                                reason=(
                                    "workload.host_memory_exhausted: ran out of memory on hardware"
                                    if evidence.failure_diagnostics
                                    and any(
                                        item.name == "exit_cause"
                                        and item.value == "host_memory_exhausted"
                                        for item in evidence.failure_diagnostics.preflight
                                    )
                                    else "workload process is not running"
                                ),
                            )
                    node.observation_unready_since = (
                        (node.observation_unready_since or observed_at)
                        if owner
                        and evidence.process_running
                        and evidence.endpoint_ready is not True
                        else None
                    )
                    node.observed_run_generation = run.run_generation
                    node.observation_process_running = evidence.process_running
                    node.observation_failure_diagnostics = (
                        evidence.failure_diagnostics.model_dump(
                            mode="json", exclude_none=True
                        )
                        if not evidence.process_running and evidence.failure_diagnostics
                        else None
                    )
                    if node.observation_failure_diagnostics is not None:
                        # Kept past the recovery that clears the observation.
                        node.last_exit_diagnostics = (
                            node.observation_failure_diagnostics
                        )
                    node.observation_observed_at = observed_at
                    node.observation_endpoint_ready = (
                        evidence.endpoint_ready if owner else None
                    )
                    node.updated_at = observed_at
                    if (
                        run.route_state == RouteState.WITHDRAWN
                        and run.next_action_at is not None
                    ):
                        schedule_wait(run, None)
                        run.updated_at = max(_now(run.updated_at).astimezone(UTC), now)
                if (
                    rejected
                    and not accepted
                    and not all(
                        reason.endswith(("is not assigned", "observation is stale"))
                        for reason in rejected
                    )
                ):
                    # Nothing in this report was usable; report the cause so
                    # the agent's log names it.  No state changed.
                    raise ValueError("; ".join(rejected[:4]))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        request.state.observation_acceptance = ObservationAcceptance(
            tuple(observed_nodes)
        )
        if rejected:
            # A report that crossed the Controller's own change (a stop, a
            # restart) is expected and dropped; it is not the agent's error.
            logging.getLogger("vonk_control.agent_api").warning(
                "agent.recipe_run_observations.partial node_id=%s accepted=%d "
                "rejected=%d first_reason=%s",
                identity.node_id,
                accepted,
                len(rejected),
                rejected[0],
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.get(
        "/recipe-runs/{run_id}/disposition",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses={
            204: {
                "description": (
                    "The run is known unless the disposition header names it unowned"
                ),
                "headers": {
                    RECIPE_RUN_DISPOSITION_HEADER: {
                        "description": "Present only as `unowned`.",
                        "schema": {"type": "string", "enum": [RECIPE_RUN_UNOWNED]},
                    },
                    RECIPE_RUN_GENERATION_HEADER: {
                        "description": (
                            "The accepted run generation of a known stoppable run."
                        ),
                        "schema": {"type": "integer", "minimum": 1},
                    },
                },
            }
        },
    )
    def recipe_run_disposition(run_id: str, request: Request) -> Response:
        """Say whether this Controller still wants one local run.

        Only a run the Controller can still stop is wanted (planned, starting,
        running, stopping, lost). Any other run, including one this Controller
        never owned (for example after its database was rebuilt) or one it
        failed or stopped, is named ``unowned``: it holds no capacity here, so
        the agent retires its local lifecycle instead of keeping it forever.
        The agent asks only once the process is gone: a process still running
        for an ended run is reported, and that report reopens the run as lost.
        """

        helper_identity(request)
        if _CANONICAL_UUID.fullmatch(run_id) is None:
            raise HTTPException(status_code=422, detail="recipe run id is invalid")
        with _require_services(services).sessions() as session:
            run = session.get(RecipeRun, run_id)
            wanted = run is not None and run.state in STOPPABLE_RUN_STATES
            # A lost run (a cancelled start left it so) is reported on too: the
            # agent's word that its process is gone releases its claims.
            generation = (
                run.run_generation
                if run is not None and run.state in {RunState.RUNNING, RunState.LOST}
                else None
            )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        if not wanted:
            response.headers[RECIPE_RUN_DISPOSITION_HEADER] = RECIPE_RUN_UNOWNED
        elif generation is not None:
            response.headers[RECIPE_RUN_GENERATION_HEADER] = str(generation)
        return response

    @agent.get(
        "/source-bundles/{source_sha256}",
        response_class=Response,
        responses=download_responses("application/vnd.vonk-forge.source-bundle.v1+tar"),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def source_bundle(source_sha256: str, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        if _DIGEST.fullmatch(source_sha256) is None:
            raise HTTPException(status_code=404, detail="source bundle does not exist")
        with required.sessions() as session:
            authorized = session.scalar(
                select(RecipeBuild.id).where(
                    RecipeBuild.builder_node_id == identity.node_id,
                    RecipeBuild.source_bundle_sha256 == source_sha256,
                    builds_in_flight(_now(required.clock()).astimezone(UTC)),
                )
            )
            if authorized is None:
                raise HTTPException(
                    status_code=404, detail="source bundle does not exist"
                )
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                bundle = required.source_bundles.get(source_sha256)
                break
            except SourceBundleError as error:
                if isinstance(error, SecurityRefusalError):
                    raise HTTPException(
                        status_code=403,
                        detail="source bundle access was denied",
                        headers={"x-vonk-error-code": error.code},
                    ) from None
                # Stored damage is not caller validation. Re-open the exact
                # authorized content; its producer may restore it independently.
                continue
        else:
            raise UnknownOutcomeError(
                "authorized source bundle observation is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return Response(
            content=bundle.archive,
            media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
            headers={
                "etag": f'"sha256:{source_sha256}"',
                "cache-control": "private, immutable, max-age=31536000",
                "x-content-type-options": "nosniff",
            },
        )
