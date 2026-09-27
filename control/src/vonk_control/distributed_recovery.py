"""Durable controller coordination for distributed recipe recovery."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import RecipeStartPayload, canonical_message
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .distributed_lifecycle import (
    DistributedLifecycleError,
    canonical_distributed_readiness,
)
from .litellm import LiteLlmGeneration
from .models import (
    AgentNode,
    AgentOperation,
    AgentPresence,
    CatalogDocumentRevision,
    ClusterMapping,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    installation_plan_document,
    run_plan_document,
)
from .recipe_start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
    validate_distributed_start_timeout_seconds,
)
from .recipe_stop_payloads import (
    RecipeStopAuthorityError,
    durable_run_stop_payloads,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SINGLETON_RECOVERY_RECHECK_SECONDS = 5

_DISTRIBUTED_START_CAPABILITY = "recipe.start.two-phase.v1"
_EXACT_RUN_INSPECTION_CAPABILITY = "recipe.run.inspect.exact.v1"


def _active_recipe_revision(
    session: Session, revision_id: str
) -> tuple[CatalogDocumentRevision, RecipeDefinition] | None:
    revision = session.get(CatalogDocumentRevision, revision_id)
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.state != "active"
    ):
        return None
    try:
        recipe = RecipeDefinition.model_validate(revision.document)
    except (TypeError, ValueError):
        return None
    if content_sha256(recipe) != revision.content_digest:
        return None
    return revision, recipe


class _RecoveryJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> AgentOperation: ...

    def notify_available(self) -> None: ...


class _RecoveryRoutes(Protocol):
    def publication_transaction(self) -> AbstractContextManager[Session]: ...

    def withdraw_run_in_session(
        self, session: Session, run_id: str
    ) -> LiteLlmGeneration: ...


class _RecoveryRunStops(Protocol):
    def queue_recovery_stop_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        recovery_context: Mapping[str, object],
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job: ...


class _RecoveryDependencyPending(Exception):
    """A recoverable precondition is not ready yet; keep the run current."""


class DistributedRecoveryCoordinator:
    """Queue one durable, bounded stop/start recovery for a failed exact rank set."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        routes: _RecoveryRoutes,
        agent_jobs: _RecoveryJobQueue,
        clock: Callable[[], datetime],
        recovery_run_stops: _RecoveryRunStops | None = None,
        singleton_start_timeout_seconds: int = 3600,
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._agent_jobs = agent_jobs
        self._clock = clock
        self._recovery_run_stops = recovery_run_stops
        self._singleton_start_timeout_seconds = (
            validate_distributed_start_timeout_seconds(singleton_start_timeout_seconds)
        )

    def tick(self) -> bool:
        now = _aware(self._clock())
        queued = False
        worked = False
        with self._routes.publication_transaction() as session:
            candidates = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == "running",
                        or_(
                            RecipeRun.route_next_attempt_at.is_(None),
                            RecipeRun.route_next_attempt_at <= now,
                        ),
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                    .with_for_update(of=RecipeRun)
                )
            )
            for run in candidates:
                failed = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id, RunNode.state == "failed")
                        .order_by(RunNode.rank)
                    )
                )
                if not failed:
                    continue
                run_nodes = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id)
                        .order_by(RunNode.rank)
                    )
                )
                singleton = len(run_nodes) == 1
                if run.route_state != "withdrawn":
                    self._routes.withdraw_run_in_session(session, run.id)
                    run.route_state = "withdrawn"
                    run.updated_at = now
                    worked = True
                if self._active_recovery(session, run.id):
                    continue
                if singleton and not _proves_fresh_absence(run, run_nodes[0], now):
                    worked = (
                        _schedule_singleton_recovery_wait(
                            run,
                            "singleton recovery waits for a fresh exact signed "
                            "absence observation",
                            now,
                        )
                        or worked
                    )
                    continue
                try:
                    run_plan = run_plan_document(run.plan)
                except RecipeExecutionContractError:
                    run.state = "failed"
                    run.route_state = "withdrawn"
                    run.route_error = "stored run plan is invalid"
                    run.updated_at = now
                    worked = True
                    continue
                try:
                    if singleton:
                        authority = _singleton_recovery_authority(
                            session,
                            run,
                            run_nodes[0],
                            now,
                            next_run_generation=run.run_generation + 1,
                            start_timeout_seconds=(
                                self._singleton_start_timeout_seconds
                            ),
                        )
                    else:
                        previous_run_generation = run.run_generation
                        run.run_generation += 1
                        run_plan["run_generation"] = run.run_generation
                        run.plan = run_plan_document(run_plan)
                        for node in run_nodes:
                            node.observed_run_generation = None
                            node.observation_receipt_sha256 = None
                            node.observation_process_running = None
                            node.observation_observed_at = None
                            node.observation_endpoint_ready = None
                        authority = _recovery_authority(
                            session,
                            run,
                            now,
                            failed[0].rank,
                            stop_run_generation=previous_run_generation,
                        )
                    if authority is None:
                        raise DistributedLifecycleError(
                            "accepted run has no automatic recovery authority"
                        )
                    if singleton:
                        run.run_generation += 1
                        run_plan["run_generation"] = run.run_generation
                        run.plan = run_plan_document(run_plan)
                        run_nodes[0].observed_run_generation = None
                        run_nodes[0].observation_receipt_sha256 = None
                        run_nodes[0].observation_process_running = None
                        run_nodes[0].observation_observed_at = None
                        run_nodes[0].observation_endpoint_ready = None
                    if singleton:
                        if self._recovery_run_stops is None:
                            raise DistributedLifecycleError(
                                "singleton recovery Stop owner is unavailable"
                            )
                        deadline = authority.get("deadline")
                        start_phases = authority.get("start_phases")
                        workload_intent_ordinal = authority.get(
                            "workload_intent_ordinal"
                        )
                        if (
                            not isinstance(deadline, str)
                            or not isinstance(start_phases, list)
                            or type(workload_intent_ordinal) is not int
                            or workload_intent_ordinal < 1
                        ):
                            raise DistributedLifecycleError(
                                "singleton recovery authority is invalid"
                            )
                        recovery_context = {
                            "schema_version": 1,
                            "failed_rank": 0,
                            "deadline": deadline,
                            "start_phases": _encode_phases(start_phases),
                        }
                        job = self._recovery_run_stops.queue_recovery_stop_in_session(
                            session,
                            run.id,
                            recovery_context=recovery_context,
                            workload_intent_ordinal=workload_intent_ordinal,
                            now=now,
                        )
                    else:
                        job = _enqueue_recovery_stop(
                            session,
                            self._agent_jobs,
                            run,
                            authority,
                            failed_rank=failed[0].rank,
                            now=now,
                        )
                except _RecoveryDependencyPending as pending:
                    worked = (
                        _schedule_singleton_recovery_wait(run, str(pending), now)
                        or worked
                    )
                    continue
                except DistributedLifecycleError as error:
                    run.state = "failed"
                    run.route_state = "withdrawn"
                    run.route_error = str(error)[:512]
                    run.route_next_attempt_at = None
                    run.updated_at = now
                    worked = True
                    continue
                run.route_state = "withdrawn"
                run.route_error = f"distributed recovery queued: {job.id}"
                run.route_next_attempt_at = None
                run.updated_at = now
                queued = True
                worked = True
                break
        if queued:
            self._agent_jobs.notify_available()
        return worked

    @staticmethod
    def _active_recovery(session: Session, run_id: str) -> bool:
        jobs = session.scalars(
            select(Job).where(
                Job.kind.in_({"recipe.start", "recipe.stop"}),
                Job.state.in_({"queued", "running"}),
            )
        )
        return any(
            job.payload.get("owner_id") == run_id
            and isinstance(job.payload.get("recovery"), Mapping)
            for job in jobs
        )


def recovery_start_plan(
    payload: Mapping[str, object], *, now: datetime
) -> (
    tuple[tuple[tuple[tuple[str, Mapping[str, object]], ...], ...], dict[str, object]]
    | None
):
    """Decode the trusted start phases carried by a recovery stop job."""

    value = payload.get("recovery")
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "failed_rank",
        "deadline",
        "start_phases",
    }:
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    enforce_recovery_deadline(payload, now=now)
    failed_rank = value["failed_rank"]
    deadline_value = value["deadline"]
    phases = _decode_phases(value.get("start_phases"))
    marker = {
        "schema_version": 1,
        "failed_rank": failed_rank,
        "deadline": deadline_value,
    }
    return phases, marker


def enforce_recovery_deadline(payload: Mapping[str, object], *, now: datetime) -> bool:
    """Validate and enforce a retained recovery marker at a trust boundary."""

    value = payload.get("recovery")
    if value is None:
        return False
    if not isinstance(value, Mapping) or set(value) not in (
        {"schema_version", "failed_rank", "deadline"},
        {"schema_version", "failed_rank", "deadline", "start_phases"},
    ):
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    failed_rank = value.get("failed_rank")
    deadline_value = value.get("deadline")
    if (
        value.get("schema_version") != 1
        or type(failed_rank) is not int
        or failed_rank < 0
        or not isinstance(deadline_value, str)
    ):
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    try:
        deadline = datetime.fromisoformat(deadline_value)
    except ValueError as error:
        raise DistributedLifecycleError(
            "distributed recovery authority is invalid"
        ) from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    if _aware(now) >= _aware(deadline):
        raise DistributedLifecycleError("distributed recovery deadline elapsed")
    return True


def _proves_fresh_absence(run: RecipeRun, node: RunNode, now: datetime) -> bool:
    """Require a current-generation signed receipt that reports no process."""

    if (
        node.state != "failed"
        or node.observed_run_generation != run.run_generation
        or node.observation_process_running is not False
        or not isinstance(node.observation_receipt_sha256, str)
        or _DIGEST.fullmatch(node.observation_receipt_sha256) is None
    ):
        return False
    try:
        run_plan = run_plan_document(run.plan)
    except RecipeExecutionContractError:
        return False
    run_nodes = run_plan.get("nodes")
    if (
        run_plan.get("observation_schema_version") != 2
        or run_plan.get("run_generation") != run.run_generation
        or run_plan.get("execution_mode") == "one-shot-jobs"
        or not isinstance(run_nodes, list)
        or len(run_nodes) != 1
        or not isinstance(run_nodes[0], Mapping)
        or run_nodes[0].get("node_id") != node.node_id
        or run_nodes[0].get("rank") != 0
        or run_nodes[0].get("role") != "entrypoint"
        or run_nodes[0].get("endpoint_owner") is not True
    ):
        return False
    if node.observation_endpoint_ready is not False or not isinstance(
        node.observation_observed_at, datetime
    ):
        return False
    observed_at = _aware(node.observation_observed_at)
    age = _aware(now) - observed_at
    return timedelta(0) <= age < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)


def _schedule_singleton_recovery_wait(
    run: RecipeRun, reason: str, now: datetime
) -> bool:
    """Persist a bounded check time without turning an unchanged wait into work."""

    changed = False
    if run.route_error != reason:
        run.route_error = reason[:512]
        changed = True
    next_attempt = run.route_next_attempt_at
    if next_attempt is None or _aware(next_attempt) <= now:
        run.route_next_attempt_at = now + timedelta(
            seconds=_SINGLETON_RECOVERY_RECHECK_SECONDS
        )
        changed = True
    if changed:
        run.updated_at = now
    return changed


def _singleton_recovery_authority(
    session: Session,
    run: RecipeRun,
    run_node: RunNode,
    now: datetime,
    *,
    next_run_generation: int,
    start_timeout_seconds: int,
) -> dict[str, object] | None:
    """Rebuild one exact accepted persistent Start after signed absence."""

    if not _proves_fresh_absence(run, run_node, now):
        return None
    installation = session.get(RecipeInstallation, run.installation_id)
    resolved = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if (
        installation is None
        or installation.state != "installed"
        or installation.image_digest is None
        or resolved is None
    ):
        raise DistributedLifecycleError(
            "singleton recovery installation authority is missing"
        )
    revision, recipe = resolved
    topology = recipe.topology.model_dump(mode="json")
    if topology.get("mode") != "single" or topology.get("node_count") != 1:
        return None
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    if (
        len(nodes) != 1
        or nodes[0].id != run_node.id
        or run_node.rank != 0
        or run_node.role != "entrypoint"
    ):
        raise DistributedLifecycleError("singleton recovery rank set is invalid")
    try:
        run_plan = run_plan_document(run.plan)
        installation_plan = installation_plan_document(installation.plan)
    except RecipeExecutionContractError as error:
        raise DistributedLifecycleError("singleton recovery plan is invalid") from error
    run_nodes = run_plan.get("nodes")
    compiled_plans = installation_plan.get("compiled_execution_plans")
    if (
        run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or run_plan.get("installation_id") != installation.id
        or run_plan.get("mapping_id") != run.mapping_id
        or run_plan.get("mapping_generation") != run.mapping_generation
        or run_plan.get("recipe_revision_id") != revision.id
        or run_plan.get("plan_digest") != run.plan_digest
        or run_plan.get("alias") != run.alias
        or run_plan.get("run_generation") != run.run_generation
        or run_plan.get("execution_mode") == "one-shot-jobs"
        or installation_plan.get("mapping_id") != installation.mapping_id
        or installation_plan.get("mapping_generation")
        != installation.mapping_generation
        or installation_plan.get("recipe_revision_id") != revision.id
        or installation_plan.get("recipe_content_sha256") != revision.content_digest
        or installation_plan.get("image_digest") != installation.image_digest
        or not isinstance(run_nodes, list)
        or len(run_nodes) != 1
        or not isinstance(run_nodes[0], Mapping)
        or not isinstance(compiled_plans, Mapping)
        or set(compiled_plans) != {run_node.node_id}
    ):
        raise DistributedLifecycleError("singleton recovery plan authority is stale")
    plan_node = run_nodes[0]
    install_compiled_plan = compiled_plans.get(run_node.node_id)
    if (
        plan_node.get("node_id") != run_node.node_id
        or plan_node.get("rank") != run_node.rank
        or plan_node.get("role") != run_node.role
        or plan_node.get("port") != run_node.port
        or plan_node.get("required_memory_bytes") != run_node.reserved_memory_bytes
        or plan_node.get("endpoint_owner") is not True
        or plan_node.get("fabric_address") is not None
        or not isinstance(install_compiled_plan, Mapping)
    ):
        raise DistributedLifecycleError("singleton recovery placement is invalid")
    memory_floor = plan_node.get("memory_floor_bytes")
    memory_kind = plan_node.get("memory_kind")
    if (
        type(memory_floor) is not int
        or memory_floor < 0
        or memory_kind not in {"unified", "host", "accelerator"}
    ):
        raise DistributedLifecycleError("singleton recovery resources are invalid")
    mapping = session.get(ClusterMapping, run.mapping_id)
    if (
        mapping is None
        or mapping.state != "ready"
        or mapping.generation != run.mapping_generation
        or mapping.endpoint_owner_node_id != run_node.node_id
    ):
        raise DistributedLifecycleError("singleton recovery mapping is stale")
    lifecycle = recipe.runtime.model_dump(mode="json").get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise DistributedLifecycleError(
            "singleton recovery lifecycle authority is invalid"
        )
    for hook_name in ("pre_start", "post_stop"):
        hooks = lifecycle.get(hook_name, [])
        if not isinstance(hooks, list):
            raise DistributedLifecycleError(
                "singleton recovery lifecycle authority is invalid"
            )
        if hooks:
            raise DistributedLifecycleError(
                f"singleton recovery does not replay the recipe {hook_name} hook"
            )
    stop_timeout = (
        lifecycle.get("stop_timeout_seconds")
        if isinstance(lifecycle, Mapping)
        else None
    )
    if type(stop_timeout) is not int or not 1 <= stop_timeout <= 600:
        raise DistributedLifecycleError("singleton recovery stop timeout is invalid")
    start_timeout = validate_distributed_start_timeout_seconds(start_timeout_seconds)
    deadline = _aware(now) + timedelta(seconds=start_timeout + stop_timeout)
    agent_node = session.get(AgentNode, run_node.node_id)
    required_capabilities = {
        "recipe.stop",
        _EXACT_RUN_INSPECTION_CAPABILITY,
        "recipe.run.inspect.receipt.v1",
    }
    if (
        agent_node is None
        or agent_node.state != "active"
        or not required_capabilities <= set(agent_node.capabilities or ())
        or not isinstance(agent_node.observation_receipt_public_key, str)
    ):
        raise DistributedLifecycleError(
            "singleton recovery requires exact Stop and signed observation support"
        )
    presence = session.scalar(
        select(AgentPresence)
        .where(AgentPresence.node_id == run_node.node_id)
        .order_by(AgentPresence.observed_at.desc())
        .limit(1)
    )
    if (
        presence is None
        or not isinstance(presence.management_address, str)
        or not timedelta(0)
        <= _aware(now) - _aware(presence.observed_at)
        < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)
    ):
        raise _RecoveryDependencyPending(
            "singleton recovery waits for a fresh Controller-observed Spark presence report"
        )
    start_job, start_ordinal = _accepted_start_authority(
        session, run, revision.content_digest, run_node.node_id
    )
    accepted_start_payload = _accepted_start_authority_payload(
        session, start_job, run_node.node_id
    )
    compiled_plan = accepted_start_payload.get("compiled_execution_plan")
    try:
        accepted_start = RecipeStartPayload.model_validate_json(
            canonical_message(accepted_start_payload)
        )
    except (TypeError, ValueError) as error:
        raise DistributedLifecycleError("accepted Start payload is invalid") from error
    compiled_plan = accepted_start_payload.get("compiled_execution_plan")
    if not isinstance(compiled_plan, Mapping):
        raise DistributedLifecycleError("accepted Start plan is invalid")
    expected_lifecycle = {
        "pre_start": lifecycle.get("pre_start", []),
        "post_stop": lifecycle.get("post_stop", []),
        "stop_timeout_seconds": stop_timeout,
    }
    for label, plan in (
        ("installed", install_compiled_plan),
        ("accepted Start", compiled_plan),
    ):
        compiled_lifecycle = plan.get("lifecycle")
        if (
            not isinstance(compiled_lifecycle, Mapping)
            or set(compiled_lifecycle) != set(expected_lifecycle)
            or canonical_message(dict(compiled_lifecycle))
            != canonical_message(expected_lifecycle)
        ):
            raise DistributedLifecycleError(
                f"singleton recovery {label} lifecycle differs from accepted recipe"
            )
    accepted_compiled_identity = compiled_plan.get("identity")
    install_compiled_identity = install_compiled_plan.get("identity")
    if (
        accepted_start.schema_version != 2
        or str(accepted_start.run_id) != run.id
        or str(accepted_start.installation_id) != installation.id
        or str(accepted_start.recipe_revision_id) != revision.id
        or accepted_start.recipe_content_sha256 != revision.content_digest
        or str(accepted_start.mapping_id) != run.mapping_id
        or accepted_start.mapping_generation != run.mapping_generation
        or accepted_start.run_generation != run.run_generation
        or accepted_start.image_digest != installation.image_digest
        or accepted_start.plan_digest != run.plan_digest
        or accepted_start.alias != run.alias
        or accepted_start.rank != run_node.rank
        or accepted_start.role != run_node.role
        or accepted_start.port != run_node.port
        or accepted_start.reserved_memory_bytes != run_node.reserved_memory_bytes
        or accepted_start.memory_floor_bytes != memory_floor
        or accepted_start.memory_kind != memory_kind
        or accepted_start.world_size != 1
        or accepted_start.local_address is not None
        or accepted_start.master_address is not None
        or accepted_start.master_port is not None
        or accepted_start.phase is not None
        or not isinstance(accepted_compiled_identity, Mapping)
        or not isinstance(install_compiled_identity, Mapping)
        or canonical_message(accepted_compiled_identity)
        != canonical_message(install_compiled_identity)
    ):
        raise DistributedLifecycleError("accepted Start image authority is stale")
    if start_job.result is not None and (
        not isinstance(start_job.result, Mapping)
        or start_job.result.get("cancel_requested") is True
    ):
        raise DistributedLifecycleError(
            "singleton recovery start authority was cancelled"
        )
    try:
        start_payload = build_recipe_start_payload(
            run_id=run.id,
            installation_id=installation.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            mapping_id=run.mapping_id,
            mapping_generation=run.mapping_generation,
            run_generation=next_run_generation,
            image_digest=installation.image_digest,
            plan_digest=run.plan_digest,
            alias=run.alias,
            placement=RecipeStartPlacement(
                run_node.node_id,
                run_node.rank,
                run_node.role,
                run_node.port,
                run_node.reserved_memory_bytes,
                memory_floor,
                memory_kind,
                None,
            ),
            endpoint_address=presence.management_address,
            compiled_endpoint_address=presence.management_address,
            world_size=1,
            compiled_execution_plan=compiled_plan,
            local_address=None,
            master_address=None,
            master_port=None,
        )
    except (KeyError, RecipeStartPayloadError) as error:
        raise DistributedLifecycleError(
            "singleton recovery start payload is invalid"
        ) from error
    return {
        "deadline": deadline.isoformat(),
        "workload_intent_ordinal": start_ordinal,
        "failed_rank": 0,
        "recipe_content_sha256": revision.content_digest,
        "start_phases": [[(run_node.node_id, start_payload)]],
    }


def _accepted_start_authority(
    session: Session, run: RecipeRun, recipe_digest: str, node_id: str
) -> tuple[Job, int]:
    starts = tuple(
        session.scalars(
            select(Job)
            .where(
                Job.kind == "recipe.start",
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
            )
            .order_by(Job.created_at, Job.id)
        )
    )
    accepted = tuple(job for job in starts if job.payload.get("recovery") is None)
    original = accepted[0] if len(accepted) == 1 else None
    targets = sorted(
        session.scalars(select(RunNode.node_id).where(RunNode.run_id == run.id))
    )
    ordinal = original.payload.get("workload_intent_ordinal") if original else None
    if (
        original is None
        or original.state != "succeeded"
        or original.authority_revision != recipe_digest
        or original.payload.get("schema_version") != 1
        or original.payload.get("owner_kind") != "run"
        or original.payload.get("owner_id") != run.id
        or original.payload.get("plan_digest") != run.plan_digest
        or original.targets != targets
        or original.payload_digest
        != hashlib.sha256(canonical_message(original.payload)).hexdigest()
        or type(ordinal) is not int
        or ordinal < 1
    ):
        raise DistributedLifecycleError(
            "singleton recovery lacks exact accepted Start authority"
        )
    if run.run_generation == 1:
        start = original
    else:
        current = tuple(
            job
            for job in starts
            if isinstance(job.payload.get("recovery"), Mapping)
            and _launch_generation(job, node_id) == run.run_generation
        )
        start = current[0] if len(current) == 1 else None
        if start is None:
            raise DistributedLifecycleError(
                "singleton recovery lacks current-generation Start authority"
            )
        _validate_singleton_recovery_start_origin(
            session,
            start,
            run=run,
            recipe_digest=recipe_digest,
            targets=targets,
            workload_intent_ordinal=ordinal,
        )
    if (
        start.state != "succeeded"
        or start.authority_revision != recipe_digest
        or start.payload.get("schema_version") != 1
        or start.payload.get("owner_kind") != "run"
        or start.payload.get("owner_id") != run.id
        or start.payload.get("plan_digest") != run.plan_digest
        or start.targets != targets
        or start.payload.get("workload_intent_ordinal") != ordinal
        or start.payload_digest
        != hashlib.sha256(canonical_message(start.payload)).hexdigest()
        or (
            isinstance(start.result, Mapping)
            and start.result.get("cancel_requested") is True
        )
    ):
        raise DistributedLifecycleError(
            "singleton recovery lacks exact current Start authority"
        )
    _accepted_start_authority_payload(session, start, node_id)
    return start, ordinal


def _launch_generation(job: Job, node_id: str) -> int | None:
    result = job.result
    evidence = result.get("launch_evidence") if isinstance(result, Mapping) else None
    launch = evidence.get(node_id) if isinstance(evidence, Mapping) else None
    generation = launch.get("run_generation") if isinstance(launch, Mapping) else None
    return generation if type(generation) is int else None


def _validate_singleton_recovery_start_origin(
    session: Session,
    start: Job,
    *,
    run: RecipeRun,
    recipe_digest: str,
    targets: list[str],
    workload_intent_ordinal: int,
) -> None:
    marker = start.payload.get("recovery")
    deadline = marker.get("deadline") if isinstance(marker, Mapping) else None
    if (
        not isinstance(marker, Mapping)
        or set(marker) != {"schema_version", "failed_rank", "deadline"}
        or marker.get("schema_version") != 1
        or marker.get("failed_rank") != 0
        or not isinstance(deadline, str)
        or start.payload.get("workload_intent_ordinal") != workload_intent_ordinal
    ):
        raise DistributedLifecycleError("singleton recovery Start marker is invalid")
    stop_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:singleton-recovery-stop:{run.id}:{run.run_generation}:{deadline}",
        )
    )
    stops = tuple(
        session.scalars(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.request_id == stop_request_id,
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
            )
        )
    )
    stop = stops[0] if len(stops) == 1 else None
    if stop is None:
        raise DistributedLifecycleError(
            "singleton recovery Start lacks its exact completed Stop"
        )
    recovery = stop.payload.get("recovery")
    if stop.state != "succeeded" or stop.actor != "system:singleton-recovery":
        raise DistributedLifecycleError(
            "singleton recovery Stop did not complete under its recovery actor"
        )
    if (
        stop.authority_revision != run.plan_digest
        or stop.targets != targets
        or stop.payload.get("workload_intent_ordinal") != workload_intent_ordinal
        or stop.payload_digest
        != hashlib.sha256(canonical_message(stop.payload)).hexdigest()
    ):
        raise DistributedLifecycleError(
            "singleton recovery Stop has stale exact run authority"
        )
    if (
        not isinstance(recovery, Mapping)
        or set(recovery)
        != {"schema_version", "failed_rank", "deadline", "start_phases"}
        or any(recovery.get(key) != marker.get(key) for key in marker)
    ):
        raise DistributedLifecycleError(
            "singleton recovery Stop continuation differs from its Start"
        )
    phases = _decode_phases(recovery.get("start_phases"))
    projected_start_phases = _project_start_phases(start.payload.get("phases"))
    if (
        projected_start_phases is None
        or canonical_message(_encode_phases(phases))
        != canonical_message(projected_start_phases)
        or start.request_id
        != str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:distributed-recovery-start:{stop.id}",
            )
        )
    ):
        raise DistributedLifecycleError(
            "singleton recovery Start differs from its exact Stop continuation"
        )


def _project_start_phases(value: object) -> list[list[dict[str, object]]] | None:
    if not isinstance(value, list) or not value:
        return None
    projected: list[list[dict[str, object]]] = []
    for raw_group in value:
        if not isinstance(raw_group, list) or not raw_group:
            return None
        group: list[dict[str, object]] = []
        for raw_item in raw_group:
            if (
                not isinstance(raw_item, Mapping)
                or not isinstance(raw_item.get("node_id"), str)
                or not isinstance(raw_item.get("payload"), Mapping)
            ):
                return None
            group.append(
                {
                    "node_id": raw_item["node_id"],
                    "payload": dict(raw_item["payload"]),
                }
            )
        projected.append(group)
    return projected


def _accepted_start_authority_payload(
    session: Session, start: Job, node_id: str
) -> Mapping[str, object]:
    phases = start.payload.get("phases")
    if not isinstance(phases, list) or not phases:
        raise DistributedLifecycleError("accepted Start payload is missing")
    items = [
        item
        for phase in phases
        if isinstance(phase, list)
        for item in phase
        if isinstance(item, Mapping) and item.get("node_id") == node_id
    ]
    if (
        sum(len(phase) for phase in phases if isinstance(phase, list)) != 1
        or len(items) != 1
        or not isinstance(items[0].get("operation_id"), str)
        or not isinstance(items[0].get("payload"), Mapping)
    ):
        raise DistributedLifecycleError("accepted Start payload is not singleton")
    child = session.get(AgentOperation, items[0]["operation_id"])
    payload = items[0]["payload"]
    if (
        child is None
        or child.parent_job_id != start.id
        or child.node_id != node_id
        or child.kind != "recipe.start"
        or child.state != "succeeded"
        or not isinstance(child.payload, Mapping)
        or canonical_message(child.payload) != canonical_message(payload)
        or child.payload_digest
        != hashlib.sha256(canonical_message(child.payload)).hexdigest()
    ):
        raise DistributedLifecycleError("accepted Start payload binding is invalid")
    return dict(child.payload)


def _recovery_authority(
    session: Session,
    run: RecipeRun,
    now: datetime,
    failed_rank: int,
    *,
    stop_run_generation: int,
) -> dict[str, object] | None:
    installation = session.get(RecipeInstallation, run.installation_id)
    resolved = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if installation is None or resolved is None or installation.image_digest is None:
        raise DistributedLifecycleError("distributed recovery authority is missing")
    revision, recipe = resolved
    topology = recipe.topology.model_dump(mode="json")
    runtime = recipe.runtime.model_dump(mode="json")
    lifecycle = runtime.get("lifecycle")
    if topology.get("mode") != "distributed" or not isinstance(lifecycle, Mapping):
        return None
    readiness = canonical_distributed_readiness(
        topology=topology,
        interfaces=[
            interface.model_dump(mode="json") for interface in recipe.interfaces
        ],
        lifecycle=lifecycle,
    )
    if readiness is None:
        return None
    failure = lifecycle.get("failure")
    if (
        not isinstance(failure, Mapping)
        or failure.get("rank_loss") != "withdraw-endpoint"
        or failure.get("recovery") != "restart-worker-then-entrypoint"
        or readiness.get("strategy") != "endpoint-owner-after-all-ranks"
    ):
        return None
    stop_timeout = lifecycle.get("stop_timeout_seconds")
    if type(stop_timeout) is not int or not 1 <= stop_timeout <= 600:
        raise DistributedLifecycleError("distributed recovery timeout is invalid")
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    if (
        tuple(node.rank for node in nodes) != tuple(range(len(nodes)))
        or len(nodes) != topology.get("node_count")
        or failed_rank not in {node.rank for node in nodes}
    ):
        raise DistributedLifecycleError("distributed recovery rank set is invalid")
    try:
        run_plan = run_plan_document(run.plan)
        installation_plan = installation_plan_document(installation.plan)
    except RecipeExecutionContractError as error:
        raise DistributedLifecycleError(
            "distributed recovery plan is invalid"
        ) from error
    if (
        run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or run_plan is None
        or run_plan.get("installation_id") != run.installation_id
        or run_plan.get("mapping_id") != run.mapping_id
        or run_plan.get("mapping_generation") != run.mapping_generation
        or run_plan.get("recipe_revision_id") != installation.recipe_revision_id
        or run_plan.get("plan_digest") != run.plan_digest
        or run_plan.get("alias") != run.alias
        or run_plan.get("run_generation") != run.run_generation
    ):
        raise DistributedLifecycleError("distributed recovery run authority is stale")
    plans = run_plan.get("nodes")
    compiled_plans = installation_plan.get("compiled_execution_plans")
    if (
        not isinstance(plans, list)
        or len(plans) != len(nodes)
        or not isinstance(compiled_plans, Mapping)
    ):
        raise DistributedLifecycleError("distributed recovery plan is invalid")
    by_rank = {item.get("rank"): item for item in plans if isinstance(item, Mapping)}
    owners = tuple(
        item
        for item in plans
        if isinstance(item, Mapping) and item.get("endpoint_owner") is True
    )
    if (
        len(by_rank) != len(nodes)
        or len(owners) != 1
        or set(compiled_plans) != {node.node_id for node in nodes}
    ):
        raise DistributedLifecycleError("distributed recovery plan is invalid")
    owner = owners[0]
    master_address = owner.get("fabric_address")
    master_port = owner.get("rendezvous_port")
    if not isinstance(master_address, str) or type(master_port) is not int:
        raise DistributedLifecycleError("distributed recovery rendezvous is invalid")
    presences: dict[str, str] = {}
    for node in nodes:
        presence = session.scalar(
            select(AgentPresence)
            .where(AgentPresence.node_id == node.node_id)
            .order_by(AgentPresence.observed_at.desc())
            .limit(1)
        )
        if presence is None or not isinstance(presence.management_address, str):
            raise DistributedLifecycleError(
                "distributed recovery endpoint evidence is missing"
            )
        presences[node.node_id] = presence.management_address
    start_job, startup_budget = _original_start_authority(
        session, run, revision.content_digest
    )
    # Rank reload/JIT needs its accepted startup duration, not the short health
    # probe timeout. Ordered stops have their own per-role execution budget.
    # Persist the total once: phase advances, retries and route publication all
    # retain this exact deadline instead of granting time again after each stop.
    stop_order = topology.get("stop_order")
    if not isinstance(stop_order, list) or not stop_order:
        raise DistributedLifecycleError("distributed recovery order is invalid")
    start_deadline = (
        now + startup_budget + timedelta(seconds=stop_timeout * len(stop_order))
    ).isoformat()
    advertised = {
        node.node_id: set(node.capabilities or ())
        for node in session.scalars(
            select(AgentNode).where(
                AgentNode.node_id.in_([run_node.node_id for run_node in nodes])
            )
        )
    }
    if any(
        not {
            _DISTRIBUTED_START_CAPABILITY,
            _EXACT_RUN_INSPECTION_CAPABILITY,
        }
        <= advertised.get(run_node.node_id, set())
        for run_node in nodes
    ):
        raise DistributedLifecycleError(
            "distributed recovery requires two-phase exact-observation agent support"
        )
    start_payloads: dict[str, tuple[str, dict[str, object]]] = {}
    for node in nodes:
        plan = by_rank[node.rank]
        compiled_plan = compiled_plans.get(node.node_id)
        local_address = plan.get("fabric_address")
        endpoint_owner = plan.get("endpoint_owner")
        memory_floor = plan.get("memory_floor_bytes")
        memory_kind = plan.get("memory_kind")
        if (
            plan.get("node_id") != node.node_id
            or plan.get("rank") != node.rank
            or plan.get("role") != node.role
            or plan.get("port") != node.port
            or plan.get("required_memory_bytes") != node.reserved_memory_bytes
            or type(memory_floor) is not int
            or memory_floor < 0
            or memory_kind not in {"unified", "host", "accelerator"}
            or not isinstance(local_address, str)
            or type(endpoint_owner) is not bool
            or not isinstance(compiled_plan, Mapping)
        ):
            raise DistributedLifecycleError("distributed recovery plan is invalid")
        try:
            payload = build_recipe_start_payload(
                run_id=run.id,
                installation_id=installation.id,
                recipe_revision_id=revision.id,
                recipe_content_sha256=revision.content_digest,
                mapping_id=run.mapping_id,
                mapping_generation=run.mapping_generation,
                run_generation=run.run_generation,
                image_digest=installation.image_digest,
                plan_digest=run.plan_digest,
                alias=run.alias,
                placement=RecipeStartPlacement(
                    node.node_id,
                    node.rank,
                    node.role,
                    node.port,
                    node.reserved_memory_bytes,
                    memory_floor,
                    memory_kind,
                    local_address,
                ),
                endpoint_address=(
                    presences[node.node_id] if endpoint_owner else local_address
                ),
                compiled_endpoint_address=(
                    presences[node.node_id] if endpoint_owner else None
                ),
                world_size=len(nodes),
                compiled_execution_plan=compiled_plan,
                local_address=local_address,
                master_address=master_address,
                master_port=master_port,
                phase="rank-launch",
                start_deadline=start_deadline,
            )
        except (KeyError, RecipeStartPayloadError) as error:
            raise DistributedLifecycleError(
                "distributed recovery start payload is invalid"
            ) from error
        start_payloads[node.role] = (node.node_id, payload)
    start_order = topology.get("start_order")
    stop_order = topology.get("stop_order")
    roles = {node.role for node in nodes}
    if (
        not isinstance(start_order, list)
        or not isinstance(stop_order, list)
        or set(start_order) != roles
        or set(stop_order) != roles
        or len(start_order) != len(roles)
        or len(stop_order) != len(roles)
    ):
        raise DistributedLifecycleError("distributed recovery order is invalid")
    owner_role = owner.get("role")
    if not isinstance(owner_role, str) or owner_role not in start_payloads:
        raise DistributedLifecycleError("distributed recovery endpoint is invalid")
    owner_node_id, owner_payload = start_payloads[owner_role]
    if type(stop_run_generation) is not int or stop_run_generation < 1:
        raise DistributedLifecycleError(
            "distributed recovery prior Start generation is invalid"
        )
    try:
        exact_stop_payloads = durable_run_stop_payloads(
            session,
            run,
            nodes,
            run_generation=stop_run_generation,
            cancel_pending_start=True,
            allow_missing_nodes=False,
        )
    except RecipeStopAuthorityError as error:
        raise DistributedLifecycleError(
            "distributed recovery lacks exact prior Start Stop authority"
        ) from error
    return {
        "deadline": start_deadline,
        "workload_intent_ordinal": start_job.payload.get("workload_intent_ordinal"),
        "failed_rank": failed_rank,
        "recipe_content_sha256": revision.content_digest,
        "start_phases": [
            [start_payloads[str(role)] for role in start_order],
            [
                (
                    owner_node_id,
                    {**owner_payload, "phase": "collective-readiness"},
                )
            ],
        ],
        "stop_phases": [
            [
                (
                    node.node_id,
                    json.loads(canonical_message(exact_stop_payloads[node.node_id])),
                )
                for node in nodes
                if node.role == role
            ]
            for role in stop_order
        ],
    }


def _enqueue_recovery_stop(
    session: Session,
    queue: _RecoveryJobQueue,
    run: RecipeRun,
    authority: Mapping[str, object],
    *,
    failed_rank: int,
    now: datetime,
) -> Job:
    raw_stop_phases = authority.get("stop_phases")
    raw_start_phases = authority.get("start_phases")
    recipe_digest = authority.get("recipe_content_sha256")
    deadline = authority.get("deadline")
    if (
        not isinstance(raw_stop_phases, list)
        or not isinstance(raw_start_phases, list)
        or not isinstance(recipe_digest, str)
        or not isinstance(deadline, str)
    ):
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    stop_phases = tuple(tuple(group) for group in raw_stop_phases)
    start_phases = tuple(tuple(group) for group in raw_start_phases)
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:distributed-recovery:{run.id}:{failed_rank}:{deadline}",
        )
    )
    if session.scalar(select(Job.id).where(Job.request_id == request_id)):
        raise DistributedLifecycleError("distributed recovery is already queued")
    job_id = str(uuid.uuid4())
    stop_phase_operations = tuple(
        tuple((str(uuid.uuid4()), node_id, payload) for node_id, payload in group)
        for group in stop_phases
    )
    job_payload = {
        "schema_version": 1,
        "owner_kind": "run",
        "owner_id": run.id,
        "plan_digest": run.plan_digest,
        "phases": [
            [
                {
                    "operation_id": operation_id,
                    "node_id": node_id,
                    "payload": json.loads(canonical_message(payload)),
                }
                for operation_id, node_id, payload in group
            ]
            for group in stop_phase_operations
        ],
        "recovery": {
            "schema_version": 1,
            "failed_rank": failed_rank,
            "deadline": deadline,
            "start_phases": _encode_phases(start_phases),
        },
    }
    targets = sorted(node_id for group in stop_phases for node_id, _payload in group)
    start_ordinal = authority.get("workload_intent_ordinal")
    target_nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(targets))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        )
    )
    if (
        type(start_ordinal) is not int
        or start_ordinal < 1
        or tuple(node.node_id for node in target_nodes) != tuple(targets)
        or any(node.workload_intent_ordinal != start_ordinal for node in target_nodes)
    ):
        raise DistributedLifecycleError(
            "distributed recovery start authority was superseded"
        )
    job_payload["workload_intent_ordinal"] = start_ordinal
    job = Job(
        id=job_id,
        request_id=request_id,
        kind="recipe.stop",
        state="running",
        actor="system:distributed-recovery",
        authority_revision=recipe_digest.removeprefix("sha256:"),
        targets=targets,
        payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
        payload=job_payload,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    for operation_id, node_id, payload in stop_phase_operations[0]:
        queue.enqueue_in_session(
            session,
            job.id,
            node_id,
            "recipe.stop",
            recipe_digest.removeprefix("sha256:"),
            payload,
            operation_id=operation_id,
        )
    return job


def _original_start_authority(
    session: Session, run: RecipeRun, recipe_digest: str | None
) -> tuple[Job, timedelta]:
    """Read the exact accepted start's budget; configuration is not a fallback."""

    starts = tuple(
        session.scalars(
            select(Job)
            .where(
                Job.kind == "recipe.start",
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
                Job.payload["recovery"].as_string().is_(None),
            )
            .order_by(Job.created_at, Job.id)
            .limit(2)
        )
    )
    if len(starts) != 1:
        raise DistributedLifecycleError(
            "distributed recovery lacks its start authority"
        )
    start = starts[0]
    deadline_value = start.payload.get("start_deadline")
    ordinal = start.payload.get("workload_intent_ordinal")
    targets = sorted(
        session.scalars(select(RunNode.node_id).where(RunNode.run_id == run.id))
    )
    if (
        start.state != "succeeded"
        or start.authority_revision != recipe_digest
        or type(ordinal) is not int
        or ordinal < 1
        or start.payload.get("plan_digest") != run.plan_digest
        or start.targets != targets
        or not isinstance(deadline_value, str)
    ):
        raise DistributedLifecycleError(
            "distributed recovery start authority is invalid"
        )
    try:
        deadline = _aware(datetime.fromisoformat(deadline_value))
        # PostgreSQL returns an aware UTC value; SQLite's test adapter drops
        # its timezone from this database-owned timestamp.
        created = start.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        duration = deadline - _aware(created)
        seconds = duration.total_seconds()
        if not seconds.is_integer():
            raise ValueError("startup budget must be whole seconds")
        validate_distributed_start_timeout_seconds(int(seconds))
    except (ValueError, DistributedLifecycleError) as error:
        raise DistributedLifecycleError(
            "distributed recovery start authority is invalid"
        ) from error
    return start, duration


def _encode_phases(
    phases: Sequence[Sequence[tuple[str, Mapping[str, object]]]],
) -> list[list[dict[str, object]]]:
    return [
        [
            {"node_id": node_id, "payload": json.loads(canonical_message(payload))}
            for node_id, payload in group
        ]
        for group in phases
    ]


def _decode_phases(
    value: object,
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...]:
    if not isinstance(value, list) or not value:
        raise DistributedLifecycleError("distributed recovery phases are invalid")
    phases: list[tuple[tuple[str, Mapping[str, object]], ...]] = []
    for raw_group in value:
        if not isinstance(raw_group, list) or not raw_group:
            raise DistributedLifecycleError("distributed recovery phases are invalid")
        group: list[tuple[str, Mapping[str, object]]] = []
        for item in raw_group:
            if not isinstance(item, Mapping) or set(item) != {"node_id", "payload"}:
                raise DistributedLifecycleError(
                    "distributed recovery phases are invalid"
                )
            node_id = item.get("node_id")
            item_payload = item.get("payload")
            if not isinstance(node_id, str) or not isinstance(item_payload, Mapping):
                raise DistributedLifecycleError(
                    "distributed recovery phases are invalid"
                )
            group.append((node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DistributedLifecycleError("distributed recovery clock is invalid")
    return value.astimezone(UTC)


__all__ = [
    "DistributedRecoveryCoordinator",
    "enforce_recovery_deadline",
    "recovery_start_plan",
]
