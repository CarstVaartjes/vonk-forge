"""Distributed recovery: authority concerns."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import RecipeStartPayload, WaitReason, canonical_message
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS

from ..distributed_lifecycle import canonical_distributed_readiness
from ..job_documents import (
    DistributedRecoveryMarker,
    RecipeStartParent,
    RecipeStopParent,
    RecoveryStartItem,
    StartPhaseOperation,
    StopPhaseOperation,
    controller_recipe_document,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.job import JobAdapter
from ..models import (
    AgentNode,
    AgentOperation,
    AgentPresence,
    CatalogDocumentRevision,
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
from ..recipe_stop_payloads import RecipeStopAuthorityError, durable_run_stop_payloads
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .common import (
    _DAMAGED,
    _MISMATCH,
    _MISSING,
    _active_recipe_revision,
    _aware,
    _encode_phases,
    _parent,
    _RecoveryAuthority,
    _RecoveryDependencyPending,
    _RecoveryJobQueue,
    _unproven,
)


def _start_binds_current_run_plan(session: Session, start: Job, run: RecipeRun) -> bool:
    """Only completed Start receipts for the run's current exact plan can seed recovery."""

    try:
        plan = parse_stored_run_plan(run.plan)
    except RecipeExecutionContractError:
        return False
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    return (
        start.state == "succeeded"
        and revision is not None
        and start.authority_revision == revision.content_digest
        and isinstance((parent := _parent(start)), RecipeStartParent)
        and parent.plan_digest == run.plan_digest
        and plan.plan_digest == run.plan_digest
        and start.payload_digest
        == hashlib.sha256(canonical_message(parent)).hexdigest()
    )


def _project_start_phases(
    phases: list[list[StartPhaseOperation]] | None,
) -> list[list[RecoveryStartItem]] | None:
    if not phases or any(not group for group in phases):
        return None
    return [
        [
            RecoveryStartItem(node_id=item.node_id, payload=item.payload)
            for item in group
        ]
        for group in phases
    ]


def _accepted_start_authority_payload(
    session: Session, start: Job, node_id: str
) -> RecipeStartPayload | Residue:
    """The exact payload of the one accepted singleton Start child of ``node_id``."""
    parent = _parent(start)
    if not isinstance(parent, RecipeStartParent) or not parent.phases:
        return _unproven(start.id, _MISSING, "accepted Start payload is missing")
    if sum(len(phase) for phase in parent.phases) != 1:
        return _unproven(start.id, _DAMAGED, "accepted Start payload is not singleton")
    items = [
        item for phase in parent.phases for item in phase if item.node_id == node_id
    ]
    if len(items) != 1:
        return _unproven(start.id, _DAMAGED, "accepted Start payload is not singleton")
    child = _accepted_start_child(session, start, node_id, items[0])
    if isinstance(child, Residue):
        return child
    payload = read_row_column(child, "payload")
    if not isinstance(payload, RecipeStartPayload):
        return _unproven(start.id, _DAMAGED, "accepted Start child payload is invalid")
    return payload


def _accepted_start_child(
    session: Session,
    start: Job,
    node_id: str,
    item: StartPhaseOperation,
) -> AgentOperation | Residue:
    child = session.get(AgentOperation, item.operation_id)
    payload = read_row_column(child, "payload") if child is not None else None
    if (
        child is None
        or child.parent_job_id != start.id
        or child.node_id != node_id
        or child.kind != "recipe.start"
        or child.state != "succeeded"
        or not isinstance(payload, RecipeStartPayload)
        or canonical_message(child.payload) != canonical_message(item.payload)
        or child.payload_digest
        != hashlib.sha256(canonical_message(child.payload)).hexdigest()
    ):
        return _unproven(
            start.id, _MISMATCH, "accepted Start payload binding is invalid"
        )
    return child


def _recovery_authority(
    session: Session,
    run: RecipeRun,
    now: datetime,
    failed_rank: int,
    *,
    stop_run_generation: int,
) -> _RecoveryAuthority | Residue | None:
    installation = session.get(RecipeInstallation, run.installation_id)
    resolved = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if installation is None or resolved is None or installation.image_digest is None:
        return _unproven(run.id, _MISSING, "distributed recovery authority is missing")
    revision, recipe = resolved
    topology = recipe.topology
    # A distributed topology withdraws its endpoint on rank loss and recovers
    # by restarting the workers and then the entrypoint.
    readiness = canonical_distributed_readiness(
        topology=topology,
        interfaces=[
            interface.model_dump(mode="json") for interface in recipe.interfaces
        ],
    )
    if readiness is None:
        return None
    stop_timeout = recipe.runtime.lifecycle.stop_timeout_seconds
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    if (
        tuple(node.rank for node in nodes) != tuple(range(len(nodes)))
        or len(nodes) != topology.node_count
        or failed_rank not in {node.rank for node in nodes}
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery rank set is invalid")
    try:
        run_plan = parse_stored_run_plan(run.plan)
        installation_plan = parse_stored_installation_plan(installation.plan)
    except RecipeExecutionContractError as error:
        return _unproven(
            run.id, _DAMAGED, "distributed recovery plan is invalid", error
        )
    if (
        run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or run_plan is None
        or run_plan.installation_id != run.installation_id
        or run_plan.mapping_id != run.mapping_id
        or run_plan.mapping_generation != run.mapping_generation
        or run_plan.recipe_revision_id != installation.recipe_revision_id
        or run_plan.plan_digest != run.plan_digest
        or run_plan.alias != run.alias
        or run_plan.run_generation != run.run_generation
    ):
        return _unproven(
            run.id, _MISMATCH, "distributed recovery run authority is stale"
        )
    plans = run_plan.nodes
    compiled_plans = installation_plan.compiled_execution_plans
    if len(plans) != len(nodes):
        return _unproven(run.id, _DAMAGED, "distributed recovery plan is invalid")
    by_rank = {item.rank: item for item in plans}
    owners = tuple(item for item in plans if item.endpoint_owner is True)
    if (
        len(by_rank) != len(nodes)
        or len(owners) != 1
        or set(compiled_plans) != {node.node_id for node in nodes}
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery plan is invalid")
    owner = owners[0]
    master_address = owner.fabric_address
    master_port = owner.rendezvous_port
    if not isinstance(master_address, str) or type(master_port) is not int:
        return _unproven(run.id, _DAMAGED, "distributed recovery rendezvous is invalid")
    presences: dict[str, str] = {}
    for node in nodes:
        presence = session.scalar(
            select(AgentPresence)
            .where(AgentPresence.node_id == node.node_id)
            .order_by(AgentPresence.observed_at.desc())
            .limit(1)
        )
        observed_at = None if presence is None else presence.observed_at
        # Stored timestamps can come back naive (SQLite); they are UTC either
        # way, matching the other stored-observation readers in this module.
        if observed_at is not None and observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        if (
            presence is None
            or not isinstance(presence.management_address, str)
            or observed_at is None
            or not timedelta(0)
            <= _aware(now) - observed_at
            < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)
        ):
            raise _RecoveryDependencyPending(
                "distributed recovery waits for a fresh Controller-observed "
                "Spark presence report",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        presences[node.node_id] = presence.management_address
    original = _original_start_authority(session, run, revision.content_digest)
    if isinstance(original, Residue):
        return original
    start_job, startup_budget = original
    # Rank reload/JIT needs its accepted startup duration, not the short health
    # probe timeout. Ordered stops have their own per-role execution budget.
    # Persist the total once: phase advances, retries and route publication all
    # retain this exact deadline instead of granting time again after each stop.
    stop_order = topology.stop_order
    start_deadline = (
        now + startup_budget + timedelta(seconds=stop_timeout * len(stop_order))
    ).isoformat()
    start_payloads: dict[str, tuple[str, RecipeStartPayload]] = {}
    for node in nodes:
        plan = by_rank[node.rank]
        compiled_plan = compiled_plans.get(node.node_id)
        local_address = plan.fabric_address
        endpoint_owner = plan.endpoint_owner
        memory_floor = plan.memory_floor_bytes
        memory_kind = plan.memory_kind
        if (
            plan.node_id != node.node_id
            or plan.rank != node.rank
            or plan.role != node.role
            or plan.port != node.port
            or plan.required_memory_bytes != node.reserved_memory_bytes
            or type(memory_floor) is not int
            or memory_floor < 0
            or memory_kind not in {"unified", "host", "accelerator"}
            or not isinstance(local_address, str)
            or type(endpoint_owner) is not bool
            or compiled_plan is None
        ):
            return _unproven(run.id, _DAMAGED, "distributed recovery plan is invalid")
        try:
            payload = build_recipe_start_payload(
                run_id=run.id,
                installation_id=installation.id,
                recipe_revision_id=revision.id,
                mapping_id=run.mapping_id,
                run_generation=run.run_generation,
                plan_digest=run.plan_digest,
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
                compiled_endpoint_address=(
                    presences[node.node_id] if endpoint_owner else None
                ),
                world_size=len(nodes),
                compiled_execution_plan=compiled_plan,
                master_address=master_address,
                master_port=master_port,
                phase="rank-launch",
                start_deadline=start_deadline,
            )
        except (KeyError, RecipeStartPayloadError) as error:
            return _unproven(
                run.id, _DAMAGED, "distributed recovery start payload is invalid", error
            )
        start_payloads[node.role] = (
            node.node_id,
            read_stored_model(
                RecipeStartPayload, canonical_message(payload), from_json=True
            ),
        )
    start_order = topology.start_order
    roles = {node.role for node in nodes}
    if (
        set(start_order) != roles
        or set(stop_order) != roles
        or len(start_order) != len(roles)
        or len(stop_order) != len(roles)
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery order is invalid")
    owner_role = owner.role
    if not isinstance(owner_role, str) or owner_role not in start_payloads:
        return _unproven(run.id, _DAMAGED, "distributed recovery endpoint is invalid")
    owner_node_id, owner_payload = start_payloads[owner_role]
    if type(stop_run_generation) is not int or stop_run_generation < 1:
        return _unproven(
            run.id, _DAMAGED, "distributed recovery prior Start generation is invalid"
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
        return _unproven(
            run.id,
            _MISSING,
            "distributed recovery lacks exact prior Start Stop authority",
            error,
        )
    start_parent = _parent(start_job)
    return _RecoveryAuthority(
        deadline=start_deadline,
        workload_intent_ordinal=start_parent.workload_intent_ordinal
        if isinstance(start_parent, RecipeStartParent)
        else None,
        failed_rank=failed_rank,
        recipe_content_sha256=revision.content_digest,
        start_phases=(
            tuple(start_payloads[str(role)] for role in start_order),
            (
                (
                    owner_node_id,
                    owner_payload.model_copy(update={"phase": "collective-readiness"}),
                ),
            ),
        ),
        stop_phases=tuple(
            tuple(
                (node.node_id, exact_stop_payloads[node.node_id])
                for node in nodes
                if node.role == role
            )
            for role in stop_order
        ),
    )


def _enqueue_recovery_stop(
    session: Session,
    queue: _RecoveryJobQueue,
    run: RecipeRun,
    authority: _RecoveryAuthority,
    *,
    failed_rank: int,
    now: datetime,
) -> Job | Residue:
    stop_phases = authority.stop_phases
    start_phases = authority.start_phases
    deadline = authority.deadline
    if not stop_phases or any(not group for group in stop_phases) or not start_phases:
        return _unproven(run.id, _DAMAGED, "distributed recovery authority is invalid")
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:distributed-recovery:{run.id}:{failed_rank}:{deadline}",
        )
    )
    queued = session.scalar(select(Job).where(Job.request_id == request_id))
    if queued is not None:
        # The request identity is the deterministic recovery of this run, failed
        # rank and deadline: a repeat is the same request, answered with its Job.
        return queued
    job_id = str(uuid.uuid4())
    stop_phase_operations = tuple(
        tuple((str(uuid.uuid4()), node_id, payload) for node_id, payload in group)
        for group in stop_phases
    )
    parent = RecipeStopParent(
        schema_version=1,
        owner_kind="run",
        owner_id=run.id,
        plan_digest=run.plan_digest,
        phases=[
            [
                StopPhaseOperation(
                    operation_id=operation_id, node_id=node_id, payload=payload
                )
                for operation_id, node_id, payload in group
            ]
            for group in stop_phase_operations
        ],
        recovery=DistributedRecoveryMarker(
            schema_version=1,
            failed_rank=failed_rank,
            deadline=deadline,
            start_phases=_encode_phases(start_phases),
        ),
    )
    targets = sorted(node_id for group in stop_phases for node_id, _payload in group)
    start_ordinal = authority.workload_intent_ordinal
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
        # A newer workload intent owns these Sparks: newer intent wins, so the
        # recorded Start is not replayed over it.
        return _unproven(
            run.id, _MISMATCH, "distributed recovery start authority was superseded"
        )
    parent = parent.model_copy(update={"workload_intent_ordinal": start_ordinal})
    job_payload = controller_recipe_document(parent)
    job = JobAdapter.new_job(
        state="running",
        id=job_id,
        request_id=request_id,
        kind="recipe.stop",
        actor="system:distributed-recovery",
        authority_revision=run.plan_digest.removeprefix("sha256:"),
        targets=targets,
        payload_digest=hashlib.sha256(canonical_message(parent)).hexdigest(),
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
            run.plan_digest.removeprefix("sha256:"),
            serialize_json_value(payload),
            operation_id=operation_id,
        )
    return job


def _original_start_authority(
    session: Session, run: RecipeRun, recipe_digest: str | None
) -> tuple[Job, timedelta] | Residue:
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
        )
    )
    starts = tuple(
        start
        for start in starts
        if start.state == "succeeded"
        and _start_binds_current_run_plan(session, start, run)
    )
    if not starts:
        return _unproven(
            run.id, _MISSING, "distributed recovery lacks its start authority"
        )
    start = starts[-1]
    parent = _parent(start)
    deadline_value = (
        parent.start_deadline if isinstance(parent, RecipeStartParent) else None
    )
    ordinal = (
        parent.workload_intent_ordinal
        if isinstance(parent, RecipeStartParent)
        else None
    )
    targets = sorted(
        session.scalars(select(RunNode.node_id).where(RunNode.run_id == run.id))
    )
    if (
        type(ordinal) is not int
        or ordinal < 1
        or start.targets != targets
        or deadline_value is None
    ):
        return _unproven(
            run.id, _DAMAGED, "distributed recovery start authority is invalid"
        )
    try:
        parsed_deadline = deadline_value
        if parsed_deadline.utcoffset() is None:
            # A stored deadline without a zone is damaged bookkeeping, not a
            # clock fault.
            return _unproven(
                run.id, _DAMAGED, "distributed recovery start authority is invalid"
            )
        deadline = _aware(parsed_deadline)
        # PostgreSQL returns an aware UTC value; SQLite's test adapter drops
        # its timezone from this database-owned timestamp.
        created = start.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        duration = deadline - _aware(created)
        seconds = duration.total_seconds()
        if not seconds.is_integer():
            return _unproven(
                run.id, _DAMAGED, "distributed recovery start authority is invalid"
            )
        validate_distributed_start_timeout_seconds(int(seconds))
    except ValueError as error:
        return _unproven(
            run.id, _DAMAGED, "distributed recovery start authority is invalid", error
        )
    return start, duration
