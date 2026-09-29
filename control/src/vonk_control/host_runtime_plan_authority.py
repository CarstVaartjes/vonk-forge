"""Derive signed lifecycle-plan bindings from durable Controller authority."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ContainerRuntimeAction, canonical_message
from vonk_agent_protocol.compiled_execution_plan import CompiledPlacement
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload

from .distributed_recovery import DistributedLifecycleError, recovery_start_plan
from .models import (
    AgentOperation,
    ArtifactJob,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .recipe_stop_payloads import (
    RecipeStopAuthorityError,
    durable_run_stop_payloads,
    stop_payload_from_job_run,
    stop_payload_from_start,
)


class RuntimePlanAuthorityError(ValueError):
    """Durable operation state cannot authorize the requested lifecycle plan."""


@dataclass(frozen=True, slots=True)
class RuntimePlanBinding:
    start_plan_sha256: str | None = None
    stop_plan_sha256: str | None = None
    run_generation: int | None = None
    runtime_run_id: str | None = None
    runtime_target_id: str | None = None
    runtime_installation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RunAuthority:
    run: RecipeRun
    installation: RecipeInstallation
    revision: CatalogDocumentRevision
    mapping: ClusterMapping
    nodes: tuple[RunNode, ...]


def derive_runtime_plan_binding(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    action: ContainerRuntimeAction,
    cancellation_requested: bool,
    now: datetime,
) -> RuntimePlanBinding:
    """Select and hash the exact plan owned by this durable child operation."""

    if action not in {ContainerRuntimeAction.START, ContainerRuntimeAction.STOP}:
        return RuntimePlanBinding()
    _validate_child_record(parent, operation, node_id)
    if operation.kind == "recipe.start":
        start = _parse_start(operation.payload)
        _service_run_authority(
            session,
            parent=parent,
            operation=operation,
            node_id=node_id,
            start=start,
            action=action,
        )
        if action is ContainerRuntimeAction.START:
            plan = start
            binding = RuntimePlanBinding(
                start_plan_sha256=_payload_sha256(plan),
                run_generation=start.run_generation,
                runtime_run_id=start.run_id,
                runtime_target_id=start.run_id,
                runtime_installation_id=start.installation_id,
            )
        else:
            stop = stop_payload_from_start(
                start,
                cancel_pending_start=cancellation_requested,
            )
            plan = stop
            binding = RuntimePlanBinding(
                stop_plan_sha256=_payload_sha256(plan),
                run_generation=stop.run_generation,
                runtime_run_id=stop.run_id,
                runtime_target_id=stop.target_runtime_id,
                runtime_installation_id=stop.installation_id,
            )
        return binding
    if operation.kind == "recipe.job.run.v1":
        job_plan = _parse_job_run(operation.payload)
        _job_run_authority(
            session,
            parent=parent,
            operation=operation,
            node_id=node_id,
            request=job_plan,
            action=action,
        )
        if action is ContainerRuntimeAction.START:
            plan = job_plan
            binding = RuntimePlanBinding(
                start_plan_sha256=_payload_sha256(plan),
                run_generation=job_plan.run_generation,
                runtime_run_id=job_plan.run_id,
                runtime_target_id=job_plan.job_id,
                runtime_installation_id=job_plan.installation_id,
            )
        else:
            plan = stop_payload_from_job_run(
                job_plan,
                cancel_pending_start=cancellation_requested,
            )
            binding = RuntimePlanBinding(
                stop_plan_sha256=_payload_sha256(plan),
                run_generation=plan.run_generation,
                runtime_run_id=plan.run_id,
                runtime_target_id=plan.target_runtime_id,
                runtime_installation_id=plan.installation_id,
            )
        return binding
    if action is not ContainerRuntimeAction.STOP or operation.kind != "recipe.stop":
        raise RuntimePlanAuthorityError("lifecycle plan owner does not match action")

    stop = _parse_stop(operation.payload)
    _service_stop_authority(
        session,
        parent=parent,
        operation=operation,
        node_id=node_id,
        stop=stop,
        now=now,
    )
    return RuntimePlanBinding(
        stop_plan_sha256=_payload_sha256(stop),
        run_generation=stop.run_generation,
        runtime_run_id=stop.run_id,
        runtime_target_id=stop.target_runtime_id,
        runtime_installation_id=stop.installation_id,
    )


def _service_run_authority(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    start: RecipeStartPayload,
    action: ContainerRuntimeAction,
) -> _RunAuthority:
    if parent.kind != "recipe.start" or parent.payload.get("owner_kind") != "run":
        raise RuntimePlanAuthorityError("service Start owner is invalid")
    run = session.get(RecipeRun, start.run_id)
    authority = _load_run_authority(session, run)
    run = authority.run
    _check_operation_target(operation, node_id, start, authority)
    if (
        parent.payload.get("owner_id") != run.id
        or parent.payload.get("plan_digest") != run.plan_digest
        or start.run_generation != run.run_generation
        or start.run_id != run.id
        or start.installation_id != run.installation_id
        or start.mapping_id != run.mapping_id
        or start.plan_digest != run.plan_digest
        or start.recipe_revision_id != authority.revision.id
        or parent.authority_revision
        != authority.revision.content_digest.removeprefix("sha256:")
    ):
        raise RuntimePlanAuthorityError("service Start identity is stale")
    try:
        stored = parse_stored_run_plan(run.plan)
    except RecipeExecutionContractError as error:
        raise RuntimePlanAuthorityError("service run plan is invalid") from error
    if (
        stored.run_generation != run.run_generation
        or stored.plan_digest != run.plan_digest
        or stored.installation_id != run.installation_id
        or stored.mapping_id != run.mapping_id
        or stored.mapping_generation != run.mapping_generation
        or stored.recipe_revision_id != authority.revision.id
        or action is ContainerRuntimeAction.START
        and (
            run.state not in {"starting", "running"}
            or authority.installation.state != "installed"
            or authority.mapping.state != "ready"
            or authority.revision.state != "active"
        )
    ):
        raise RuntimePlanAuthorityError("service Start is no longer authorized")
    return authority


def _job_run_authority(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    request: RecipeJobRunRequest,
    action: ContainerRuntimeAction,
) -> _RunAuthority:
    if (
        parent.kind != "recipe.job.run.v1"
        or parent.payload.get("owner_kind") != "artifact-job"
        or parent.payload.get("owner_id") != request.job_id
    ):
        raise RuntimePlanAuthorityError("artifact JobRun owner is invalid")
    artifact_job = session.get(ArtifactJob, request.job_id)
    run = session.get(RecipeRun, request.run_id)
    authority = _load_run_authority(session, run)
    run = authority.run
    _check_operation_target(operation, node_id, request, authority)
    if (
        artifact_job is None
        or artifact_job.run_id != run.id
        or artifact_job.operation_id != parent.id
        or parent.payload.get("plan_digest") != run.plan_digest
        or request.installation_id != run.installation_id
        or request.mapping_id != run.mapping_id
        or request.plan_digest != run.plan_digest
        or request.recipe_revision_id != authority.revision.id
        or parent.authority_revision
        != authority.revision.content_digest.removeprefix("sha256:")
    ):
        raise RuntimePlanAuthorityError("artifact JobRun identity is stale")
    if action is ContainerRuntimeAction.START and (
        request.run_generation != run.run_generation
        or run.state != "running"
        or authority.installation.state != "installed"
        or authority.mapping.state != "ready"
        or authority.revision.state != "active"
    ):
        raise RuntimePlanAuthorityError("artifact JobRun start is no longer authorized")
    # JobRun cleanup keeps its immutable request generation. The distinct
    # artifact job ID is the runtime target, so an old job cleanup cannot stop
    # the service container created by a newer RecipeRun generation.
    return authority


def _service_stop_authority(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    stop: RecipeStopPayload,
    now: datetime,
) -> None:
    if parent.kind != "recipe.stop" or parent.payload.get("owner_kind") != "run":
        raise RuntimePlanAuthorityError("service Stop owner is invalid")
    run = session.get(RecipeRun, stop.run_id)
    authority = _load_run_authority(session, run)
    run = authority.run
    if (
        stop.target_runtime_id != run.id
        or stop.installation_id != run.installation_id
        or stop.mapping_id != run.mapping_id
        or stop.plan_digest != run.plan_digest
        or stop.recipe_revision_id != authority.revision.id
        or parent.payload.get("owner_id") != run.id
        or parent.authority_revision != run.plan_digest.removeprefix("sha256:")
    ):
        raise RuntimePlanAuthorityError("service Stop identity is stale")
    try:
        stored = parse_stored_run_plan(run.plan)
        exact = durable_run_stop_payloads(
            session,
            run,
            authority.nodes,
            run_generation=stop.run_generation,
            cancel_pending_start=stop.cancel_pending_start,
            allow_missing_nodes=False,
        )
    except (RecipeExecutionContractError, RecipeStopAuthorityError) as error:
        raise RuntimePlanAuthorityError(
            "durable prior Start authority is unavailable"
        ) from error
    recovery_marker = parent.payload.get("recovery")
    if isinstance(recovery_marker, Mapping):
        if stop.run_generation + 1 != run.run_generation:
            raise RuntimePlanAuthorityError("recovery Stop generation is stale")
        _validate_recovery_start_generation(
            parent=parent,
            authority=authority,
            current_generation=run.run_generation,
            now=now,
        )
    elif stop.run_generation != run.run_generation:
        raise RuntimePlanAuthorityError("service Stop generation is stale")
    expected = exact.get(node_id)
    if (
        expected is None
        or canonical_message(stop) != canonical_message(expected)
        or stored.run_generation != run.run_generation
        or operation.kind != "recipe.stop"
    ):
        raise RuntimePlanAuthorityError("service Stop differs from exact prior Start")


def _validate_recovery_start_generation(
    *,
    parent: Job,
    authority: _RunAuthority,
    current_generation: int,
    now: datetime,
) -> None:
    try:
        recovered = recovery_start_plan(
            parent.payload, now=now, require_unexpired=False
        )
        stored = parse_stored_run_plan(authority.run.plan)
    except (DistributedLifecycleError, RecipeExecutionContractError) as error:
        raise RuntimePlanAuthorityError("recovery Start plan is invalid") from error
    if recovered is None or stored.run_generation != current_generation:
        raise RuntimePlanAuthorityError("recovery Start generation is unavailable")
    phases, marker = recovered
    expected_nodes = {node.node_id: node for node in authority.nodes}
    if (
        parent.payload.get("owner_kind") != "run"
        or parent.payload.get("owner_id") != authority.run.id
        or parent.targets != sorted(expected_nodes)
    ):
        raise RuntimePlanAuthorityError("recovery Stop owner is stale")

    if parent.actor == "system:singleton-recovery":
        expected_request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk:singleton-recovery-stop:"
                f"{authority.run.id}:{current_generation}:{marker['deadline']}",
            )
        )
        if (
            marker["failed_rank"] != 0
            or parent.request_id != expected_request_id
            or len(expected_nodes) != 1
            or len(phases) != 1
            or len(phases[0]) != 1
        ):
            raise RuntimePlanAuthorityError("singleton recovery Stop is invalid")
        topology = "singleton"
    elif parent.actor == "system:distributed-recovery":
        failed_rank = marker["failed_rank"]
        expected_request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:distributed-recovery:{authority.run.id}:"
                f"{failed_rank}:{marker['deadline']}",
            )
        )
        if (
            type(failed_rank) is not int
            or failed_rank not in {node.rank for node in authority.nodes}
            or parent.request_id != expected_request_id
            or len(phases) != 2
            or not phases[0]
            or not phases[1]
        ):
            raise RuntimePlanAuthorityError("distributed recovery Stop is invalid")
        topology = "distributed"
    else:
        raise RuntimePlanAuthorityError("recovery Stop actor is invalid")

    rank_launches: dict[str, RecipeStartPayload] = {}
    readiness: list[tuple[str, RecipeStartPayload]] = []
    try:
        for phase_index, phase in enumerate(phases):
            for node_id, raw_payload in phase:
                start = RecipeStartPayload.model_validate_json(
                    canonical_message(raw_payload)
                )
                if (
                    start.run_generation != current_generation
                    or start.run_id != authority.run.id
                    or start.installation_id != authority.run.installation_id
                    or start.mapping_id != authority.run.mapping_id
                    or start.plan_digest != authority.run.plan_digest
                    or start.recipe_revision_id != authority.revision.id
                    or not any(
                        node.node_id == node_id
                        and node.rank == _placement(start).rank
                        and node.role == _placement(start).role
                        for node in stored.nodes
                    )
                ):
                    raise ValueError("recovery Start identity is inconsistent")
                if topology == "singleton" and phase_index == 0 and start.phase is None:
                    if node_id in rank_launches:
                        raise ValueError("singleton recovery target is duplicated")
                    rank_launches[node_id] = start
                elif (
                    topology == "distributed"
                    and phase_index == 0
                    and start.phase == "rank-launch"
                ):
                    if node_id in rank_launches:
                        raise ValueError("recovery Start target is duplicated")
                    rank_launches[node_id] = start
                elif (
                    topology == "distributed"
                    and phase_index == 1
                    and start.phase == "collective-readiness"
                ):
                    readiness.append((node_id, start))
                else:
                    raise ValueError("recovery Start phase is invalid")
    except (TypeError, ValueError) as error:
        raise RuntimePlanAuthorityError("recovery Start plan is invalid") from error
    if set(rank_launches) != set(expected_nodes):
        raise RuntimePlanAuthorityError("recovery Start target set is invalid")
    if topology == "singleton":
        node = next(iter(expected_nodes.values()))
        stored_node = next(
            item for item in stored.nodes if item.node_id == node.node_id
        )
        start = rank_launches[node.node_id]
        if (
            node.rank != 0
            or node.role != "entrypoint"
            or not stored_node.endpoint_owner
            or _placement_key(start) != (0, "entrypoint", 1)
        ):
            raise RuntimePlanAuthorityError(
                "singleton recovery Start placement is invalid"
            )
        return

    owners = [node for node in stored.nodes if node.endpoint_owner]
    if len(readiness) != 1 or len(owners) != 1 or readiness[0][0] != owners[0].node_id:
        raise RuntimePlanAuthorityError("recovery Start target set is invalid")
    for node_id, start in rank_launches.items():
        node = expected_nodes[node_id]
        if _placement_key(start) != (
            node.rank,
            node.role,
            len(expected_nodes),
        ):
            raise RuntimePlanAuthorityError("recovery Start placement is invalid")
    readiness_node = expected_nodes[readiness[0][0]]
    readiness_start = readiness[0][1]
    if _placement_key(readiness_start) != (
        readiness_node.rank,
        readiness_node.role,
        len(expected_nodes),
    ):
        raise RuntimePlanAuthorityError("recovery readiness placement is invalid")


def _placement(start: RecipeStartPayload) -> CompiledPlacement:
    return start.compiled_execution_plan.runtime.placement


def _placement_key(start: RecipeStartPayload) -> tuple[int, str, int]:
    placement = _placement(start)
    return placement.rank, placement.role, placement.world_size


def _load_run_authority(session: Session, run: RecipeRun | None) -> _RunAuthority:
    if run is None:
        raise RuntimePlanAuthorityError("recipe run is unavailable")
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    mapping = session.get(ClusterMapping, run.mapping_id)
    nodes = tuple(
        session.scalars(
            select(RunNode)
            .where(RunNode.run_id == run.id)
            .order_by(RunNode.rank, RunNode.node_id)
        )
    )
    mapping_nodes = (
        tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping.id)
                .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
            )
        )
        if mapping is not None
        else ()
    )
    try:
        stored = parse_stored_run_plan(run.plan)
    except RecipeExecutionContractError as error:
        raise RuntimePlanAuthorityError("recipe run plan is invalid") from error
    if (
        installation is None
        or revision is None
        or mapping is None
        or not nodes
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or mapping.generation != run.mapping_generation
        or mapping.recipe_revision_id != installation.recipe_revision_id
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.content_digest is None
        or stored.run_generation != run.run_generation
        or stored.installation_id != run.installation_id
        or stored.mapping_id != run.mapping_id
        or stored.mapping_generation != run.mapping_generation
        or stored.recipe_revision_id != installation.recipe_revision_id
        or stored.plan_digest != run.plan_digest
        or {(node.node_id, node.rank, node.role) for node in nodes}
        != {(node.node_id, node.rank, node.role) for node in stored.nodes}
        or {(node.node_id, node.rank, node.role) for node in nodes}
        != {(node.node_id, node.rank, node.role) for node in mapping_nodes}
    ):
        raise RuntimePlanAuthorityError("recipe run authority is stale")
    return _RunAuthority(run, installation, revision, mapping, nodes)


def _check_operation_target(
    operation: AgentOperation,
    node_id: str,
    payload: RecipeStartPayload | RecipeJobRunRequest,
    authority: _RunAuthority,
) -> None:
    node = next((item for item in authority.nodes if item.node_id == node_id), None)
    if (
        node is None
        or operation.node_id != node_id
        or payload.run_id != authority.run.id
        or payload.compiled_execution_plan.runtime.placement.rank != node.rank
        or payload.compiled_execution_plan.runtime.placement.role != node.role
        or payload.compiled_execution_plan.runtime.placement.world_size
        != len(authority.nodes)
    ):
        raise RuntimePlanAuthorityError("lifecycle plan node identity differs")


def _validate_child_record(
    parent: Job, operation: AgentOperation, node_id: str
) -> None:
    if (
        operation.node_id != node_id
        or operation.parent_job_id != parent.id
        or hashlib.sha256(canonical_message(parent.payload)).hexdigest()
        != parent.payload_digest
        or hashlib.sha256(canonical_message(operation.payload)).hexdigest()
        != operation.payload_digest
        or operation.authority_revision != parent.authority_revision
    ):
        raise RuntimePlanAuthorityError("lifecycle operation record is inconsistent")
    phases = parent.payload.get("phases")
    if not isinstance(phases, list):
        raise RuntimePlanAuthorityError("lifecycle operation manifest is missing")
    matches = [
        item
        for phase in phases
        if isinstance(phase, list)
        for item in phase
        if isinstance(item, Mapping) and item.get("operation_id") == operation.id
    ]
    if (
        len(matches) != 1
        or matches[0].get("node_id") != node_id
        or not isinstance(matches[0].get("payload"), Mapping)
        or canonical_message(matches[0]["payload"])
        != canonical_message(operation.payload)
    ):
        raise RuntimePlanAuthorityError("lifecycle operation manifest differs")


def _parse_start(value: object) -> RecipeStartPayload:
    try:
        if not isinstance(value, Mapping):
            raise TypeError("Start payload is not an object")
        return RecipeStartPayload.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise RuntimePlanAuthorityError("durable Start plan is invalid") from error


def _parse_job_run(value: object) -> RecipeJobRunRequest:
    try:
        if not isinstance(value, Mapping):
            raise TypeError("JobRun payload is not an object")
        return RecipeJobRunRequest.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise RuntimePlanAuthorityError("durable JobRun plan is invalid") from error


def _parse_stop(value: object) -> RecipeStopPayload:
    try:
        if not isinstance(value, Mapping):
            raise TypeError("Stop payload is not an object")
        return RecipeStopPayload.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise RuntimePlanAuthorityError("durable Stop plan is invalid") from error


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_message(payload)).hexdigest()
