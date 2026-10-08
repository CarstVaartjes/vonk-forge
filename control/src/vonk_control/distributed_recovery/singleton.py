"""Distributed recovery: singleton concerns."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationState,
    RecipeStartPayload,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS

from ..job_documents import RecipeStartParent, RecipeStopParent
from ..lifecycle.evidence import Residue
from ..models import (
    AgentNode,
    AgentOperation,
    AgentPresence,
    ClusterMapping,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
    parse_stored_run_plan,
)
from ..recipe_start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
    validate_distributed_start_timeout_seconds,
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model
from .authority import (
    _accepted_start_authority_payload,
    _project_start_phases,
    _start_binds_current_run_plan,
)
from .common import (
    _DAMAGED,
    _MISMATCH,
    _MISSING,
    _active_recipe_revision,
    _aware,
    _cancelled,
    _decode_phases,
    _encode_phases,
    _parent,
    _proves_fresh_absence,
    _RecoveryAuthority,
    _RecoveryDependencyPending,
    _unproven,
)


def _singleton_recovery_authority(
    session: Session,
    run: RecipeRun,
    run_node: RunNode,
    now: datetime,
    *,
    next_run_generation: int,
    start_timeout_seconds: int,
) -> _RecoveryAuthority | Residue | None:
    """Rebuild one exact accepted persistent Start after observed absence."""

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
        or installation.state != InstallationState.INSTALLED
        or installation.image_digest is None
        or resolved is None
    ):
        return _unproven(
            run.id, _MISSING, "singleton recovery installation authority is missing"
        )
    revision, recipe = resolved
    if recipe.topology.distributed:
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
        return _unproven(run.id, _DAMAGED, "singleton recovery rank set is invalid")
    try:
        run_plan = parse_stored_run_plan(run.plan)
        installation_plan = parse_stored_installation_plan(installation.plan)
    except RecipeExecutionContractError as error:
        return _unproven(run.id, _DAMAGED, "singleton recovery plan is invalid", error)
    run_nodes = run_plan.nodes
    compiled_plans = installation_plan.compiled_execution_plans
    if (
        run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or run_plan.installation_id != installation.id
        or run_plan.mapping_id != run.mapping_id
        or run_plan.mapping_generation != run.mapping_generation
        or run_plan.recipe_revision_id != revision.id
        or run_plan.plan_digest != run.plan_digest
        or run_plan.alias != run.alias
        or run_plan.run_generation != run.run_generation
        or run_plan.execution_mode == "one-shot-jobs"
        or installation_plan.mapping_id != installation.mapping_id
        or installation_plan.mapping_generation != installation.mapping_generation
        or installation_plan.recipe_revision_id != revision.id
        or installation_plan.recipe_content_sha256 != revision.content_digest
        or installation_plan.image_digest != installation.image_digest
        or len(run_nodes) != 1
        or set(compiled_plans) != {run_node.node_id}
    ):
        return _unproven(
            run.id, _MISMATCH, "singleton recovery plan authority is stale"
        )
    plan_node = run_nodes[0]
    install_compiled_plan = compiled_plans.get(run_node.node_id)
    if (
        plan_node.node_id != run_node.node_id
        or plan_node.rank != run_node.rank
        or plan_node.role != run_node.role
        or plan_node.port != run_node.port
        or plan_node.required_memory_bytes != run_node.reserved_memory_bytes
        or plan_node.endpoint_owner is not True
        or plan_node.fabric_address is not None
        or install_compiled_plan is None
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery placement is invalid")
    memory_floor = plan_node.memory_floor_bytes
    memory_kind = plan_node.memory_kind
    if (
        type(memory_floor) is not int
        or memory_floor < 0
        or memory_kind not in {"unified", "host", "accelerator"}
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery resources are invalid")
    mapping = session.get(ClusterMapping, run.mapping_id)
    if (
        mapping is None
        or mapping.state != "ready"
        or mapping.generation != run.mapping_generation
        or mapping.endpoint_owner_node_id != run_node.node_id
    ):
        return _unproven(run.id, _MISMATCH, "singleton recovery mapping is stale")
    stop_timeout = recipe.runtime.lifecycle.stop_timeout_seconds
    start_timeout = validate_distributed_start_timeout_seconds(start_timeout_seconds)
    deadline = _aware(now) + timedelta(seconds=start_timeout + stop_timeout)
    agent_node = session.get(AgentNode, run_node.node_id)
    if agent_node is None or agent_node.state != "active":
        return _unproven(
            run.id,
            _MISSING if agent_node is None else _MISMATCH,
            "singleton recovery requires exact Stop and observation support",
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
            "singleton recovery waits for a fresh Controller-observed Spark presence report",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    accepted = _accepted_start_authority(
        session, run, revision.content_digest, run_node.node_id
    )
    if isinstance(accepted, Residue):
        return accepted
    start_job, start_ordinal = accepted
    accepted_start_payload = _accepted_start_authority_payload(
        session, start_job, run_node.node_id
    )
    if isinstance(accepted_start_payload, Residue):
        return accepted_start_payload
    accepted_start = accepted_start_payload
    compiled_plan = accepted_start.compiled_execution_plan
    for label, plan in (
        ("installed", install_compiled_plan),
        ("accepted Start", compiled_plan),
    ):
        if plan.lifecycle.stop_timeout_seconds != stop_timeout:
            return _unproven(
                run.id,
                _MISMATCH,
                f"singleton recovery {label} lifecycle differs from accepted recipe",
            )
    accepted_compiled_identity = compiled_plan.identity
    install_compiled_identity = install_compiled_plan.identity
    accepted_placement = accepted_start.compiled_execution_plan.runtime.placement
    if (
        str(accepted_start.run_id) != run.id
        or str(accepted_start.installation_id) != installation.id
        or str(accepted_start.recipe_revision_id) != revision.id
        or str(accepted_start.mapping_id) != run.mapping_id
        or accepted_start.run_generation != run.run_generation
        # The Job-level binding selects the Start; the exact per-node payload
        # that recovery replays must name the same run plan.
        or accepted_start.plan_digest != run.plan_digest
        or (
            accepted_placement.rank,
            accepted_placement.role,
            accepted_placement.port,
            accepted_placement.reserved_memory_bytes,
            accepted_placement.memory_floor_bytes,
            accepted_placement.world_size,
        )
        != (
            run_node.rank,
            run_node.role,
            run_node.port,
            run_node.reserved_memory_bytes,
            memory_floor,
            1,
        )
        or accepted_start.phase is not None
        or canonical_message(accepted_compiled_identity)
        != canonical_message(install_compiled_identity)
    ):
        return _unproven(run.id, _MISMATCH, "accepted Start image authority is stale")
    if _cancelled(start_job):
        return _unproven(
            run.id, _MISMATCH, "singleton recovery start authority was cancelled"
        )
    try:
        start_payload = build_recipe_start_payload(
            run_id=run.id,
            installation_id=installation.id,
            recipe_revision_id=revision.id,
            mapping_id=run.mapping_id,
            run_generation=next_run_generation,
            plan_digest=run.plan_digest,
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
            compiled_endpoint_address=presence.management_address,
            world_size=1,
            compiled_execution_plan=WireCompiledExecutionPlan.parse(compiled_plan),
            master_address=None,
            master_port=None,
        )
    except (KeyError, RecipeStartPayloadError) as error:
        return _unproven(
            run.id, _DAMAGED, "singleton recovery start payload is invalid", error
        )
    return _RecoveryAuthority(
        deadline=deadline.isoformat(),
        workload_intent_ordinal=start_ordinal,
        failed_rank=0,
        recipe_content_sha256=revision.content_digest,
        start_phases=(
            (
                (
                    run_node.node_id,
                    read_stored_model(
                        RecipeStartPayload,
                        canonical_message(start_payload),
                        from_json=True,
                    ),
                ),
            ),
        ),
    )


def _accepted_start_authority(
    session: Session,
    run: RecipeRun,
    recipe_digest: str,
    node_id: str,
) -> tuple[Job, int] | Residue:
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
    accepted = tuple(
        job
        for job in starts
        if isinstance((parent := _parent(job)), RecipeStartParent)
        and parent.recovery is None
        and job.state == "succeeded"
        and _start_binds_current_run_plan(session, job, run)
    )
    original = accepted[-1] if accepted else None
    targets = sorted(
        session.scalars(select(RunNode.node_id).where(RunNode.run_id == run.id))
    )
    original_parent = _parent(original) if original is not None else None
    ordinal = (
        original_parent.workload_intent_ordinal
        if isinstance(original_parent, RecipeStartParent)
        else None
    )
    if (
        original is None
        or not isinstance(original_parent, RecipeStartParent)
        or original_parent.owner_kind != "run"
        or original_parent.owner_id != run.id
        or original.targets != targets
        or type(ordinal) is not int
        or ordinal < 1
    ):
        return _unproven(
            run.id, _MISSING, "singleton recovery lacks exact accepted Start authority"
        )
    if run.run_generation == 1:
        start = original
    else:
        current = tuple(
            job
            for job in starts
            if isinstance((parent := _parent(job)), RecipeStartParent)
            and parent.recovery is not None
            and job.state == "succeeded"
            and _start_binds_current_run_plan(session, job, run)
            and _launch_generation(session, job, node_id) == run.run_generation
        )
        start = current[-1] if current else None
        if start is None:
            return _unproven(
                run.id,
                _MISSING,
                "singleton recovery lacks current-generation Start authority",
            )
        origin = _validate_singleton_recovery_start_origin(
            session,
            start,
            run=run,
            recipe_digest=recipe_digest,
            targets=targets,
            workload_intent_ordinal=ordinal,
        )
        if isinstance(origin, Residue):
            return origin
    start_parent = _parent(start)
    if (
        not isinstance(start_parent, RecipeStartParent)
        or start_parent.owner_kind != "run"
        or start_parent.owner_id != run.id
        or start.targets != targets
        or start_parent.workload_intent_ordinal != ordinal
        or _cancelled(start)
    ):
        return _unproven(
            run.id, _MISSING, "singleton recovery lacks exact current Start authority"
        )
    payload = _accepted_start_authority_payload(session, start, node_id)
    if isinstance(payload, Residue):
        return payload
    return start, ordinal


def _launch_generation(session: Session, job: Job, node_id: str) -> int | None:
    """The run generation the node's fenced Start of this job launched."""
    operation = session.scalar(
        select(AgentOperation)
        .where(
            AgentOperation.parent_job_id == job.id,
            AgentOperation.node_id == node_id,
            AgentOperation.kind == "recipe.start",
            AgentOperation.state == "succeeded",
        )
        .order_by(AgentOperation.created_at.desc(), AgentOperation.id.desc())
        .limit(1)
    )
    payload = read_row_column(operation, "payload") if operation is not None else None
    return payload.run_generation if isinstance(payload, RecipeStartPayload) else None


def _validate_singleton_recovery_start_origin(
    session: Session,
    start: Job,
    *,
    run: RecipeRun,
    recipe_digest: str,
    targets: list[str],
    workload_intent_ordinal: int,
) -> Residue | None:
    start_parent = _parent(start)
    marker = (
        start_parent.recovery if isinstance(start_parent, RecipeStartParent) else None
    )
    deadline = marker.deadline if marker is not None else None
    if (
        marker is None
        or marker.start_phases is not None
        or marker.failed_rank != 0
        or not isinstance(start_parent, RecipeStartParent)
        or start_parent.workload_intent_ordinal != workload_intent_ordinal
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery Start marker is invalid")
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
        return _unproven(
            run.id, _MISSING, "singleton recovery Start lacks its exact completed Stop"
        )
    stop_parent = _parent(stop)
    recovery = (
        stop_parent.recovery if isinstance(stop_parent, RecipeStopParent) else None
    )
    if stop.state != "succeeded" or stop.actor != "system:singleton-recovery":
        return _unproven(
            run.id,
            _MISMATCH,
            "singleton recovery Stop did not complete under its recovery actor",
        )
    if (
        stop.authority_revision != run.plan_digest
        or stop.targets != targets
        or not isinstance(stop_parent, RecipeStopParent)
        or stop_parent.workload_intent_ordinal != workload_intent_ordinal
        or stop.payload_digest
        != hashlib.sha256(canonical_message(stop_parent)).hexdigest()
    ):
        return _unproven(
            run.id, _MISMATCH, "singleton recovery Stop has stale exact run authority"
        )
    if (
        recovery is None
        or recovery.start_phases is None
        or recovery.failed_rank != marker.failed_rank
        or recovery.deadline != marker.deadline
    ):
        return _unproven(
            run.id,
            _MISMATCH,
            "singleton recovery Stop continuation differs from its Start",
        )
    phases = _decode_phases(recovery.start_phases)
    if phases is None:
        return _unproven(
            run.id, _DAMAGED, "singleton recovery Stop continuation is invalid"
        )
    projected_start_phases = _project_start_phases(start_parent.phases)
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
        return _unproven(
            run.id,
            _MISMATCH,
            "singleton recovery Start differs from its exact Stop continuation",
        )
    return None
