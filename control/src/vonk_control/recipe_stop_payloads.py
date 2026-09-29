"""Canonical Controller projections for exact recipe runtime Stop authority."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload

from .models import (
    AgentOperation,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_action_plans import StopNodeImpact
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .recipe_operation_phases import StoredPhases, decode_stored_phases
from .strict_json import read_stored_model


class RecipeStopAuthorityError(ValueError):
    """A durable recipe start cannot authorize an exact runtime Stop."""


def stop_payload_from_start(
    value: RecipeStartPayload | Mapping[str, object],
    *,
    cancel_pending_start: bool,
) -> RecipeStopPayload:
    """Project one exact Stop target from its complete typed Start effect."""

    try:
        start = read_stored_model(
            RecipeStartPayload, canonical_message(value), from_json=True
        )
        if type(start.run_generation) is not int or start.run_generation < 1:
            raise ValueError("start generation is missing")
        payload = RecipeStopPayload(
            run_id=start.run_id,
            target_runtime_id=start.run_id,
            installation_id=start.installation_id,
            recipe_revision_id=start.recipe_revision_id,
            mapping_id=start.mapping_id,
            plan_digest=start.plan_digest,
            compiled_execution_plan=start.compiled_execution_plan,
            cancel_pending_start=cancel_pending_start,
            run_generation=start.run_generation,
        )
        return read_stored_model(
            RecipeStopPayload, canonical_message(payload), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise RecipeStopAuthorityError("recipe Start payload is invalid") from error


def stop_payload_from_job_run(
    value: RecipeJobRunRequest | Mapping[str, object],
    *,
    cancel_pending_start: bool,
) -> RecipeStopPayload:
    """Project a transient artifact-container Stop from its typed job request."""

    try:
        job = read_stored_model(
            RecipeJobRunRequest, canonical_message(value), from_json=True
        )
        if type(job.run_generation) is not int or job.run_generation < 1:
            raise ValueError("job run generation is missing")
        payload = RecipeStopPayload(
            run_id=job.run_id,
            target_runtime_id=job.job_id,
            installation_id=job.installation_id,
            recipe_revision_id=job.recipe_revision_id,
            mapping_id=job.mapping_id,
            plan_digest=job.plan_digest,
            compiled_execution_plan=job.compiled_execution_plan,
            cancel_pending_start=cancel_pending_start,
            run_generation=job.run_generation,
        )
        return read_stored_model(
            RecipeStopPayload, canonical_message(payload), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise RecipeStopAuthorityError("recipe JobRun payload is invalid") from error


def durable_run_stop_payloads(
    session: Session,
    run: RecipeRun,
    nodes: Sequence[RunNode | StopNodeImpact],
    *,
    run_generation: int,
    cancel_pending_start: bool,
    allow_missing_nodes: bool,
) -> dict[str, RecipeStopPayload]:
    """Recover exact Stop plans from the durable Start operation for each node.

    The operation payload is accepted only when its digest and its parent Job
    phase entry still agree. Two durable Starts at one generation are safe to
    reuse only when their canonical Stop projection is identical.
    """

    if type(run_generation) is not int or run_generation < 1:
        raise RecipeStopAuthorityError("recipe Stop generation is invalid")
    requested = {node.node_id: node for node in nodes}
    if not requested:
        raise RecipeStopAuthorityError("recipe Stop target set is empty")

    installation = session.get(RecipeInstallation, run.installation_id)
    mapping = session.get(ClusterMapping, run.mapping_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if (
        installation is None
        or mapping is None
        or revision is None
        or run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or mapping.generation != run.mapping_generation
        or mapping.recipe_revision_id != installation.recipe_revision_id
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.content_digest is None
    ):
        raise RecipeStopAuthorityError("recipe Stop identity is stale")
    mapping_nodes = tuple(
        session.scalars(
            select(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id == mapping.id)
            .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
        )
    )
    if {(item.node_id, item.rank, item.role) for item in mapping_nodes} != {
        (node.node_id, node.rank, node.role) for node in nodes
    }:
        raise RecipeStopAuthorityError("recipe Stop mapping membership differs")
    try:
        stored_run_plan = parse_stored_run_plan(run.plan)
    except RecipeExecutionContractError as error:
        raise RecipeStopAuthorityError("recipe Stop run plan is invalid") from error
    if (
        stored_run_plan.installation_id != run.installation_id
        or stored_run_plan.mapping_id != run.mapping_id
        or stored_run_plan.mapping_generation != run.mapping_generation
        or stored_run_plan.recipe_revision_id != installation.recipe_revision_id
        or stored_run_plan.plan_digest != run.plan_digest
        or {(item.node_id, item.rank, item.role) for item in stored_run_plan.nodes}
        != {(node.node_id, node.rank, node.role) for node in nodes}
    ):
        raise RecipeStopAuthorityError("recipe Stop run plan identity differs")

    jobs = tuple(
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
    if not jobs:
        raise RecipeStopAuthorityError("recipe Stop lacks durable Start authority")
    candidates: dict[str, list[RecipeStopPayload]] = defaultdict(list)
    expected_authority = revision.content_digest.removeprefix("sha256:")
    expected_targets = sorted(requested)
    for parent in jobs:
        if (
            parent.kind != "recipe.start"
            or parent.payload.get("schema_version") != 1
            or parent.payload.get("owner_kind") != "run"
            or parent.payload.get("owner_id") != run.id
            or parent.payload.get("plan_digest") != run.plan_digest
            or parent.targets != expected_targets
            or parent.authority_revision != expected_authority
            or hashlib.sha256(canonical_message(parent.payload)).hexdigest()
            != parent.payload_digest
        ):
            raise RecipeStopAuthorityError("recipe Start parent is inconsistent")
        try:
            phases: StoredPhases = decode_stored_phases(parent.payload)
        except (TypeError, ValueError) as error:
            raise RecipeStopAuthorityError("recipe Start phases are invalid") from error
        if not phases:
            raise RecipeStopAuthorityError("recipe Start phases are missing")

        planned: dict[str, tuple[str, Mapping[str, object]]] = {}
        current_starts: dict[str, RecipeStartPayload] = {}
        for phase in phases:
            for operation_id, node_id, raw_payload in phase:
                if operation_id in planned:
                    raise RecipeStopAuthorityError(
                        "recipe Start phase identities overlap"
                    )
                planned[operation_id] = (node_id, raw_payload)
                raw_run_id = raw_payload.get("run_id")
                raw_generation = raw_payload.get("run_generation")
                if (
                    raw_run_id == run.id
                    and type(raw_generation) is int
                    and raw_generation >= 1
                    and raw_generation != run_generation
                ):
                    # A separately identifiable generation remains historical
                    # data. Its child is still checked against this exact
                    # digest-bound phase entry below.
                    continue
                try:
                    start = read_stored_model(
                        RecipeStartPayload,
                        canonical_message(raw_payload),
                        from_json=True,
                    )
                except (TypeError, ValueError) as error:
                    raise RecipeStopAuthorityError(
                        "recipe Start payload is invalid"
                    ) from error
                if start.run_id != run.id:
                    raise RecipeStopAuthorityError("recipe Start run identity differs")
                if start.run_generation != run_generation:
                    continue
                node = requested.get(node_id)
                if (
                    node is None
                    or start.installation_id != run.installation_id
                    or start.recipe_revision_id != installation.recipe_revision_id
                    or start.compiled_execution_plan.identity.recipe_revision_sha256
                    != revision.content_digest
                    or start.mapping_id != run.mapping_id
                    or start.plan_digest != run.plan_digest
                    or start.compiled_execution_plan.runtime.placement.rank != node.rank
                    or start.compiled_execution_plan.runtime.placement.role != node.role
                    or start.compiled_execution_plan.runtime.placement.world_size
                    != len(requested)
                ):
                    raise RecipeStopAuthorityError(
                        "recipe Start target identity differs"
                    )
                current_starts[operation_id] = start

        phase_ids = set(planned)
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.id.in_(phase_ids))
                .order_by(AgentOperation.id)
            )
        )
        children_by_id = {child.id: child for child in children}
        children_for_parent = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == parent.id)
                .order_by(AgentOperation.id)
            )
        )
        if (
            len(children_by_id) != len(children)
            or any(child.id not in phase_ids for child in children_for_parent)
            or any(
                (child.parent_job_id != parent.id)
                for child in children
                if child.id in phase_ids
            )
        ):
            raise RecipeStopAuthorityError(
                "recipe Start children differ from retained phases"
            )
        for phase_index, phase in enumerate(phases):
            present = 0
            for operation_id, node_id, raw_payload in phase:
                child = children_by_id.get(operation_id)
                if child is None:
                    continue
                present += 1
                if (
                    child.parent_job_id != parent.id
                    or child.kind != "recipe.start"
                    or child.node_id != node_id
                    or child.authority_revision != parent.authority_revision
                    or canonical_message(child.payload)
                    != canonical_message(raw_payload)
                    or hashlib.sha256(canonical_message(child.payload)).hexdigest()
                    != child.payload_digest
                ):
                    raise RecipeStopAuthorityError(
                        "recipe Start child differs from retained phase"
                    )
                start = current_starts.get(operation_id)
                if start is not None:
                    node = requested[node_id]
                    candidates[node_id].append(
                        stop_payload_from_start(
                            start,
                            cancel_pending_start=cancel_pending_start,
                        )
                    )
            # Queue insertion creates phase zero atomically; later phases are
            # materialized together only after their predecessor succeeds.
            if (phase_index == 0 and present != len(phase)) or (
                phase_index > 0 and present not in {0, len(phase)}
            ):
                raise RecipeStopAuthorityError(
                    "recipe Start phase materialization is incomplete"
                )
        # An undispatched later phase is still a target because its complete
        # typed payload is inside the whole parent digest. Add it after checking
        # every materialized child against that exact source.
        for operation_id, start in current_starts.items():
            node_id, _payload = planned[operation_id]
            if operation_id not in children_by_id:
                node = requested[node_id]
                candidates[node_id].append(
                    stop_payload_from_start(
                        start,
                        cancel_pending_start=cancel_pending_start,
                    )
                )

    selected: dict[str, RecipeStopPayload] = {}
    for node_id, payloads in candidates.items():
        canonical = {canonical_message(payload) for payload in payloads}
        if len(canonical) != 1:
            raise RecipeStopAuthorityError(
                "recipe Start target has ambiguous durable plans"
            )
        selected[node_id] = payloads[0]
    missing = set(requested) - set(selected)
    if missing and not allow_missing_nodes:
        raise RecipeStopAuthorityError(
            "recipe Stop lacks an exact Start target for every node"
        )
    return selected


def _parent_contains_operation(parent: Job, operation: AgentOperation) -> bool:
    phases = parent.payload.get("phases")
    if not isinstance(phases, list):
        return False
    matches = []
    for phase in phases:
        if not isinstance(phase, list):
            continue
        for item in phase:
            if (
                not isinstance(item, Mapping)
                or item.get("operation_id") != operation.id
            ):
                continue
            matches.append(item)
    return (
        len(matches) == 1
        and matches[0].get("node_id") == operation.node_id
        and isinstance(matches[0].get("payload"), Mapping)
        and canonical_message(matches[0]["payload"])
        == canonical_message(operation.payload)
    )
