"""Typed recipe parent reads and progress attribution."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .recipe_operations import RecipeParent, _PhaseGroups

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import (
    OperationMemberProgress,
    OperationProgress,
    RecipeBuildCleanupRequest,
    RecipeInstallPayload,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStopPayload,
    RecipeUninstallPayload,
    canonical_message,
)
from vonk_forge_contracts import RecipeDefinition, read_model

from .distributed_lifecycle import (
    DistributedLifecycleError,
    canonical_distributed_readiness,
)
from .job_documents import (
    DistributedRecoveryMarker,
    RecipeBuildParent,
    RecipeJobActivateParent,
    RecipeReconcileParent,
    RecipeStartParent,
    RecipeStopParent,
)
from .lifecycle.evidence import (
    Damaged,
    Residue,
    read_or_rebuild,
)
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
)
from .operation_progress import (
    aggregate_progress,
    member_progress,
    project_progress_for_state,
    stored_progress,
)
from .profile_stop_authority import (
    ProfileJobRunStopJob,
)
from .recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from .recipe_runtime_specs import recipe_topology
from .run_switch_contract import (
    RunSwitchReconciliationAuthority,
)
from .strict_json import read_stored_model, serialize_json_value


def _parse_recipe_parent(job: Job) -> RecipeParent:
    from .recipe_operations import _RECIPE_PARENT_READERS

    parent = _RECIPE_PARENT_READERS[job.kind].validate_json(
        canonical_message(job.payload)
    )
    if isinstance(parent, ProfileJobRunStopJob):
        return ProfileJobRunStopJob.model_validate_parent(job.payload)
    return parent


def _recorded_parent(job: Job) -> RecipeParent | Residue:

    return read_or_rebuild(
        kind="recipe.operation-parent",
        subject=job.id,
        read=lambda: _parse_recipe_parent(job),
    )


def _parent_identity(
    job: Job, key: Literal["owner_id", "owner_kind", "plan_digest"]
) -> str | None:
    """Identity from the canonical parent, or unknown for damaged bookkeeping."""
    from .recipe_operations import _RECIPE_WIRE_PAYLOAD_MODELS

    parent = _recorded_parent(job)
    if isinstance(parent, Residue):
        # Phase history is bookkeeping. Rebuild only the exact affected owner
        # from relational child rows with current canonical payloads; this does
        # not restore review digests, executable phases, or security authority.
        if key != "owner_id":
            return None
        session = object_session(job)
        reader = _RECIPE_WIRE_PAYLOAD_MODELS.get(job.kind)
        if session is None or reader is None:
            return None
        owners: set[str] = set()
        try:
            for child in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            ):
                if child.kind != job.kind or child.node_id not in job.targets:
                    return None
                wire = reader.model_validate_json(canonical_message(child.payload))
                if isinstance(wire, (RecipeStartPayload, RecipeStopPayload)):
                    owners.add(wire.run_id)
                elif isinstance(
                    wire,
                    (
                        RecipeInstallPayload,
                        RecipeUninstallPayload,
                        RecipeReconcilePayload,
                    ),
                ):
                    owners.add(wire.installation_id)
                elif isinstance(wire, RecipeBuildCleanupRequest):
                    owners.add(wire.build_id)
                else:
                    return None
        except (TypeError, ValueError):
            return None
        return owners.pop() if len(owners) == 1 else None
    if key == "owner_id":
        return parent.owner_id
    return parent.owner_kind if key == "owner_kind" else parent.plan_digest


def _parent_intent(job: Job) -> int | None:
    parent = _recorded_parent(job)
    return None if isinstance(parent, Residue) else parent.workload_intent_ordinal


def _parent_execution_mode(
    job: Job,
) -> Literal["one-shot-jobs", "profile-jobrun-stop"] | None:
    parent = _recorded_parent(job)
    return None if isinstance(parent, Residue) else parent.execution_mode


def _parent_force_rebuild(job: Job) -> bool:
    parent = _recorded_parent(job)
    return isinstance(parent, RecipeBuildParent) and parent.force_rebuild is True


def _parent_recovery(job: Job) -> DistributedRecoveryMarker | None:
    parent = _recorded_parent(job)
    return (
        parent.recovery
        if isinstance(parent, (RecipeStartParent, RecipeStopParent))
        else None
    )


def _parent_reconciliation(job: Job) -> RunSwitchReconciliationAuthority | None:
    parent = _recorded_parent(job)
    return (
        parent.reconciliation_authority
        if isinstance(parent, RecipeReconcileParent)
        else None
    )


def _cancel_requested(job: Job) -> bool:
    from .recipe_operations import _recorded_result

    return isinstance(
        _recorded_result(job.kind, job.result, subject=job.id),
        RecipeOperationCancellationResult,
    )


def _lower_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _catalog_recipe(document: object) -> RecipeDefinition | None:
    if isinstance(document, RecipeDefinition):
        return document
    try:
        return RecipeDefinition.model_validate_json(
            canonical_message(document), extra="ignore"
        )
    except (TypeError, ValueError):
        return None


def _primary_model_identity(document: object) -> tuple[str, str] | None:
    """The canonical primary model identity, or unknown for unreadable content."""
    recipe = _catalog_recipe(document)
    if recipe is None or not recipe.models:
        return None
    model = recipe.models[0].model
    return model.content_sha256, f"{model.publisher}/{model.slug}"


def _recipe_model_identities(
    session: Session,
    document: object,
) -> tuple[tuple[str, str], ...] | None:
    """Every model a recipe needs (its own and the dependencies of those).

    ``None`` when the closure cannot be read from the catalog: the caller then
    keeps whatever it cannot prove unused.  A model reached twice is one model.
    """

    recipe = _catalog_recipe(document)
    if recipe is None or not recipe.models:
        return None
    result: list[tuple[str, str]] = []
    pending = [selection.model for selection in recipe.models]
    seen: set[tuple[str, str, str]] = set()
    while pending:
        reference = pending.pop(0)
        key = (reference.publisher, reference.slug, reference.content_sha256)
        if key in seen:
            continue
        seen.add(key)
        revision = session.scalar(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.publisher == reference.publisher,
                CatalogDocumentRevision.slug == reference.slug,
                CatalogDocumentRevision.content_digest == reference.content_sha256,
                CatalogDocumentRevision.state == "active",
            )
            .limit(1)
        )
        if revision is None or not isinstance(revision.document, Mapping):
            return None
        try:
            model = read_model(revision.document)
        except (TypeError, ValueError):
            return None
        result.append(
            (reference.content_sha256, f"{reference.publisher}/{reference.slug}")
        )
        pending.extend(model.dependencies)
    unique = {digest: (digest, title) for digest, title in result}
    return tuple(unique.values())


def _topology_order(
    document: object, key: Literal["start_order", "stop_order"]
) -> tuple[str, ...] | None:
    """The recipe's role order, or ``None`` when its topology cannot be read."""

    try:
        topology = recipe_topology(document)
    except Exception:  # noqa: BLE001 - any unreadable topology is "no order"
        return None
    return tuple(topology.start_order if key == "start_order" else topology.stop_order)


def _canonical_distributed_readiness(document: object) -> bool:
    from .recipe_operations import RecipeRequestInvalid

    recipe = _catalog_recipe(document)
    if recipe is None:
        raise RecipeRequestInvalid("distributed readiness interface is invalid")
    try:
        readiness = canonical_distributed_readiness(
            topology=recipe.topology,
            interfaces=tuple(
                serialize_json_value(interface) for interface in recipe.interfaces
            ),
        )
    except DistributedLifecycleError as error:
        raise RecipeRequestInvalid(str(error)) from error
    return readiness is not None


def _start_deadline_failure(job: Job, *, now: datetime) -> str | None:
    """The accepted start budget, with damaged bookkeeping ended by recovery."""
    from .recipe_operations import _aware

    if job.kind != "recipe.start":
        return None
    parent = _recorded_parent(job)
    if not isinstance(parent, RecipeStartParent):
        return "distributed start deadline is invalid"
    if parent.start_deadline is not None and _aware(now) >= _aware(
        parent.start_deadline
    ):
        return "distributed start deadline elapsed"
    return None


def _role_phases(
    order: Sequence[str],
    node_payloads: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...] | None:
    """Group node payloads into role phases; ``None`` when the roles do not
    match the recipe's order (the caller decides whether to order or refuse)."""

    by_role: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    reader = TypeAdapter(RecipeStartPayload | RecipeStopPayload)
    for node_id, payload in node_payloads:
        try:
            wire = reader.validate_json(canonical_message(payload))
        except (TypeError, ValueError):
            return None
        role = (
            wire.role
            if isinstance(wire, RecipeStopPayload)
            else wire.compiled_execution_plan.runtime.placement.role
        )
        by_role.setdefault(role, []).append((node_id, dict(payload)))
    if set(by_role) != set(order) or len(set(order)) != len(order):
        return None
    return tuple(
        tuple(sorted(by_role[role], key=lambda item: item[0])) for role in order
    )


def _stored_phases(job: Job) -> _PhaseGroups | Residue:
    """Read the canonical parent and its typed phase payloads; damage is unknown."""
    from .recipe_operations import RecipeWirePayload

    def read() -> _PhaseGroups | Damaged:
        parent = _parse_recipe_parent(job)
        if isinstance(parent, RecipeJobActivateParent):
            return ()
        if parent.phases is None:
            return ()
        if not parent.phases or any(not phase for phase in parent.phases):
            return Damaged("stored operation phases are invalid")
        seen_operations: set[str] = set()
        groups: list[tuple[tuple[str, str, RecipeWirePayload], ...]] = []
        for phase in parent.phases:
            group: list[tuple[str, str, RecipeWirePayload]] = []
            for item in phase:
                if item.operation_id in seen_operations:
                    return Damaged("stored operation phases are invalid")
                seen_operations.add(item.operation_id)
                group.append((item.operation_id, item.node_id, item.payload))
            groups.append(tuple(group))
        return tuple(groups)

    return read_or_rebuild(kind="recipe.operation-phases", subject=job.id, read=read)


def _current_phase_index(
    children: Sequence[AgentOperation],
    phases: _PhaseGroups,
) -> int | None:

    child_operations = {child.id for child in children}
    for index in range(len(phases) - 1, -1, -1):
        phase_operations = {
            operation_id for operation_id, _node_id, _payload in phases[index]
        }
        if phase_operations <= child_operations:
            return index
    return None


def current_recipe_progress_attribution(
    session: Session, job: Job, *, now: datetime
) -> (
    tuple[OperationProgress, tuple[tuple[AgentOperation, AgentOperationAttempt], ...]]
    | None
):
    """Read the canonical projection with the exact native samples that produced it.

    This is evidence only. It neither advances nor authorizes a child. Callers
    committing derived state must lock/recheck these same native rows themselves.
    """
    progress = _project_recipe_operation_progress(session, job, now=now)
    if progress is None:
        return None
    rows = tuple(
        session.execute(
            select(AgentOperation, AgentOperationAttempt)
            .join(
                AgentOperationAttempt,
                (AgentOperationAttempt.operation_id == AgentOperation.id)
                & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
            )
            .where(AgentOperation.parent_job_id == job.id)
            .order_by(AgentOperation.node_id, AgentOperation.id)
        )
    )
    measured = tuple(
        (operation, attempt)
        for operation, attempt in rows
        if stored_progress(attempt) is not None
    )
    if not measured:
        return None
    return progress, measured


def _project_recipe_operation_progress(
    session: Session, job: Job, *, now: datetime
) -> OperationProgress | None:
    """Read exact current-attempt samples without advancing a child or its parent."""
    from .recipe_operations import _TERMINAL_JOB_STATES, RecipeWirePayload

    with session.no_autoflush:
        rows = tuple(
            session.execute(
                select(AgentOperation, AgentOperationAttempt)
                .outerjoin(
                    AgentOperationAttempt,
                    (AgentOperationAttempt.operation_id == AgentOperation.id)
                    & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
                )
                .where(AgentOperation.parent_job_id == job.id)
                .order_by(AgentOperation.node_id, AgentOperation.id)
            )
        )
    if not rows:
        return None
    children = tuple(child for child, _attempt in rows)
    attempts = {child.id: attempt for child, attempt in rows}
    phases = _stored_phases(job)
    if isinstance(phases, Residue):
        return None
    targets = set(job.targets)
    phase_nodes = {
        operation_id: node_id
        for group in phases
        for operation_id, node_id, _payload in group
    }
    if (
        len(targets) != len(job.targets)
        or any(child.node_id not in targets for child in children)
        or (
            phases
            and (
                set(phase_nodes.values()) != targets
                or any(phase_nodes.get(child.id) != child.node_id for child in children)
            )
        )
    ):
        # Unreadable ownership does not authorize showing another member's
        # measurements, and never blocks the operation's normal recovery.
        return None
    phase_indices = {
        operation_id: index
        for index, group in enumerate(phases)
        for operation_id, _node_id, _payload in group
    }
    if phases:
        payloads = {
            identity: payload for group in phases for identity, _node, payload in group
        }
        for child in children:
            expected = payloads[child.id]
            if child.kind != job.kind:
                return None
            try:
                observed = read_stored_model(
                    type(expected), canonical_message(child.payload), from_json=True
                )
            except (TypeError, ValueError):
                return None
            if (
                canonical_message(observed) != canonical_message(expected)
                or child.payload_digest
                != hashlib.sha256(canonical_message(child.payload)).hexdigest()
            ):
                return None
    current_index = _current_phase_index(children, phases) if phases else None
    if phases and current_index is None:
        return None
    if current_index is not None and any(
        phase_indices[child.id] > current_index for child in children
    ):
        # A partially issued later group is not a complete current phase.
        return None
    current_group = phases[current_index] if current_index is not None else ()
    current_ids = {identity for identity, _node, _payload in current_group}
    current_payloads = {node: payload for _identity, node, payload in current_group}
    if len(current_payloads) != len(current_group):
        return None
    terminal = job.state in _TERMINAL_JOB_STATES
    future_payloads: dict[str, RecipeWirePayload] = {}
    if phases and not terminal:
        for group in phases[(current_index or 0) + 1 :]:
            for _identity, node, payload in group:
                future_payloads.setdefault(node, payload)
    by_node: dict[str, list[AgentOperation]] = {}
    for child in children:
        by_node.setdefault(child.node_id, []).append(child)
    members: list[OperationMemberProgress] = []
    summary: list[OperationMemberProgress] = []
    for node in sorted(targets):
        node_children = by_node.get(node, [])
        candidates = (
            [child for child in node_children if child.id in current_ids]
            if phases and not terminal
            else node_children
        )
        if phases and terminal and candidates:
            last = max(phase_indices[child.id] for child in candidates)
            candidates = [
                child for child in candidates if phase_indices[child.id] == last
            ]
        if len(candidates) > 1:
            return None
        child = candidates[0] if candidates else None
        payload = current_payloads.get(node) or future_payloads.get(node)
        phase = (
            payload.phase
            if isinstance(payload, RecipeStartPayload) and payload.phase is not None
            else job.kind
        )
        current = child is not None and (not phases or child.id in current_ids)
        attempt = attempts.get(child.id) if child is not None else None
        measured = (
            stored_progress(attempt)
            if attempt is not None and (current or terminal)
            else None
        )
        if measured is not None and child is not None:
            measured = project_progress_for_state(measured, child.state, now)
        state = child.state if child is not None else "queued" if payload else "unknown"
        member = member_progress(
            measured,
            member_id=node,
            state=state,
            phase=phase,
            kind=child.kind if child is not None else job.kind,
        )
        members.append(member)
        # Preserve older terminal samples for inspection, without summing
        # sequential phases or carrying their rates into the latest phase.
        summary.append(
            member
            if current or not phases
            else member_progress(
                None,
                member_id=node,
                state=state,
                phase="unknown",
                kind=member.kind,
            )
        )
    projected = aggregate_progress(summary)
    return projected.model_copy(update={"members": members})
