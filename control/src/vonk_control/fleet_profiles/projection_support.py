"""Projection support for Fleet profiles."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    DesiredAssignmentState,
    InvalidRequestReason,
    LifecycleState,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    ProfileChildPhase,
    ProfileEffectState,
    ProfileProjectionKind,
    ProfileSwitchChildKind,
)
from vonk_forge_contracts import RecipeDefinition, RecipeOptionError
from vonk_forge_contracts.recipe import RecipeTopology

from ..fleet_profile_contract import (
    MAX_PROFILE_WARNINGS,
    FleetProfileApplicationCancellationView,
    FleetProfileApplicationEffect,
    FleetProfileApplicationProgress,
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileChildResult,
    FleetProfileDefinition,
    FleetProfileEffects,
    FleetProfilePreview,
    FleetProfileReviewedDecision,
    FleetProfileRunEffect,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchQueueItem,
    FleetProfileView,
)
from ..lifecycle.evidence import Residue
from ..models import (
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
)
from ..stored_json import read_row_column
from .contracts import (
    FleetProfileReviewStale,
    FleetProfileStalePlanConflict,
    FleetProfileUnsupportedStore,
    SavedProfileDocument,
)


def _state_receipt(
    state: FleetProfileSwitchAdapterState,
) -> FleetProfileChildResult | None:
    for child in reversed(state.children):
        if child.result is not None:
            return child.result
    return state.result


def _application_cancellation_view(
    application: FleetProfileApplication,
    plan: FleetProfilePreview,
    progress: FleetProfileApplicationProgress,
) -> FleetProfileApplicationCancellationView | None:
    """Project effect receipts from the canonical profile progress tree."""

    intent = progress.cancellation
    if intent is None:
        return None
    completed: list[FleetProfileApplicationEffect] = []
    pending: list[FleetProfileApplicationEffect] = []
    cancelled: list[FleetProfileApplicationEffect] = []
    adapter = progress.switch_adapter
    if adapter is None:
        for key, receipt in sorted(progress.step_results.items()):
            completed.append(
                FleetProfileApplicationEffect(
                    effect_id=f"step:{key}",
                    kind=ProfileProjectionKind.STEP.value,
                    label=f"Completed profile step {key}",
                    operation_id=receipt.operation_id,
                    outcome=LifecycleState.SUCCEEDED.value,
                )
            )
        for step in plan.steps[application.current_step :]:
            cancelled.append(
                FleetProfileApplicationEffect(
                    effect_id=f"step:{step.index}",
                    kind=ProfileProjectionKind.STEP.value,
                    label=f"Not issued profile step {step.index + 1}: {step.label}",
                    outcome=ProfileEffectState.NOT_ISSUED.value,
                )
            )
        pending_ids = intent.pending_operation_ids
    else:
        for child in adapter.children:
            effect = FleetProfileApplicationEffect(
                effect_id=child.operation_id,
                kind=child.kind,
                label=f"{child.kind} child receipt",
                operation_id=child.operation_id,
                outcome=(
                    LifecycleState.CANCELLED.value
                    if child.state == LifecycleState.CANCELLED.value
                    else LifecycleState.FAILED.value
                    if child.state == LifecycleState.FAILED.value
                    else LifecycleState.SUCCEEDED.value
                ),
            )
            (
                cancelled
                if child.state == LifecycleState.CANCELLED.value
                else completed
            ).append(effect)
        active_ids = {child.operation_id for child in adapter.pending_children}
        for child in adapter.pending_children:
            pending.append(
                FleetProfileApplicationEffect(
                    effect_id=child.operation_id,
                    kind=child.kind,
                    label="Pending Run/Switch child",
                    operation_id=child.operation_id,
                    outcome=ProfileEffectState.PENDING.value,
                )
            )
        pending_ids = sorted(
            set(adapter.pending_operation_ids) | set(intent.pending_operation_ids)
        )
        for operation_id in pending_ids:
            if operation_id in active_ids:
                continue
            pending.append(
                FleetProfileApplicationEffect(
                    effect_id=operation_id,
                    kind=ProfileProjectionKind.AGENT_OPERATION.value,
                    label="Issued agent effect awaiting its receipt",
                    operation_id=operation_id,
                    outcome=ProfileEffectState.PENDING.value,
                )
            )
        occupied = {
            child.queue_index
            for child in (*adapter.pending_children, *adapter.children)
        } | set(adapter.skipped_indices)
        for index, item in enumerate(adapter.queue):
            if index in occupied:
                continue
            cancelled.append(
                FleetProfileApplicationEffect(
                    effect_id=f"queue:{index}:{item.kind}:{item.id}",
                    kind=item.kind,
                    label=f"Not issued {item.kind} effect {item.id}",
                    outcome=ProfileEffectState.NOT_ISSUED.value,
                )
            )
        # A later whole-profile step can remain after the adapter's current
        # switch queue; expose that reviewed work as not issued too.
        for step in plan.steps[application.current_step + 1 :]:
            cancelled.append(
                FleetProfileApplicationEffect(
                    effect_id=f"step:{step.index}",
                    kind=ProfileProjectionKind.STEP.value,
                    label=f"Not issued profile step {step.index + 1}: {step.label}",
                    outcome=ProfileEffectState.NOT_ISSUED.value,
                )
            )

    pending.extend(
        FleetProfileApplicationEffect(
            effect_id=operation_id,
            kind=ProfileProjectionKind.AGENT_OPERATION.value,
            label="Issued agent effect awaiting its receipt",
            operation_id=operation_id,
            outcome=ProfileEffectState.PENDING.value,
        )
        for operation_id in pending_ids
        if operation_id not in {effect.effect_id for effect in pending}
    )
    dependency = (
        adapter.pending_children[0].operation_id
        if adapter is not None and adapter.pending_children
        else None
    )
    if dependency is not None:
        owner = "run-switch"
        deadline = None
    elif pending:
        owner = "agent-operation-reconciliation"
        dependency = pending[0].operation_id
        deadline = (
            adapter.observation_deadline_at
            if adapter is not None
            else intent.observation_deadline_at
        )
    else:
        owner = None
        deadline = None
    return FleetProfileApplicationCancellationView(
        request_key=intent.request_key,
        actor=intent.actor,
        requested_at=intent.requested_at,
        state=intent.state,
        cause=intent.cause,
        completed_effects=sorted(completed, key=lambda effect: effect.effect_id),
        pending_effects=sorted(pending, key=lambda effect: effect.effect_id),
        cancelled_effects=sorted(cancelled, key=lambda effect: effect.effect_id),
        owner=owner,
        dependency=dependency,
        deadline_at=deadline,
    )


def _installation_member_ids(session: Session, installation_id: str) -> tuple[str, ...]:
    """The authoritative complete placement of one installation."""

    return tuple(
        sorted(
            node.node_id
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _review_effects_digest(decision: FleetProfileReviewedDecision) -> str:
    """Digest what an operator consents to, never what the Controller observes.

    The effects are the saved intent it applies (profile revision and digest,
    the resolved placements and options), the workloads it keeps, stops,
    replaces and installs, and the switch and preparation steps. Free capacity,
    readiness and reuse of already-prepared assets, blockers, reasons and
    timestamps are observations: a review stays valid across them, and the
    Controller still refuses or parks an inadmissible plan on its own.
    """

    return _digest(
        {
            "profile_id": decision.profile_id,
            "profile_digest": decision.profile_digest,
            "profile_revision": decision.profile_revision,
            "scope": decision.scope.node_ids,
            "assignments": [
                {
                    "assignment_id": item.assignment_id,
                    "recipe_revision_id": item.recipe_revision_id,
                    "desired_state": item.desired_state,
                    "node_ids": item.node_ids,
                    "option_choices": item.option_choices,
                    "actions": item.actions,
                }
                for item in decision.assignments
            ],
            "resolved_assignments": [
                item.model_dump(mode="json") for item in decision.resolved_assignments
            ],
            "effects": decision.effects.model_dump(mode="json"),
            "steps": [(step.kind, step.node_ids) for step in decision.steps],
            "preparation_steps": [
                (step.kind, step.node_ids) for step in decision.preparation_steps
            ],
        }
    )


def _roster_digest(node_ids: Sequence[str]) -> str:
    return _digest({"node_ids": sorted(node_ids)})


def _set_selected_profile(
    session: Session,
    *,
    profile_id: str,
    profile_revision: int,
    application_id: str,
    node_ids: Sequence[str],
    now: datetime,
) -> int:
    """Select a whole-fleet application and advance its durable generation."""

    values = {
        "singleton_id": 1,
        "generation": 1,
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "application_id": application_id,
        "roster_digest": _roster_digest(node_ids),
        "updated_at": now,
    }
    table = FleetProfileSelection.__table__
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise FleetProfileUnsupportedStore(
            "Fleet profile selection requires PostgreSQL or SQLite",
            reason=InvalidRequestReason.UNSUPPORTED,
        )
    statement = insert(FleetProfileSelection).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.singleton_id],
        set_={
            "generation": table.c.generation + 1,
            "profile_id": statement.excluded.profile_id,
            "profile_revision": statement.excluded.profile_revision,
            "application_id": statement.excluded.application_id,
            "roster_digest": statement.excluded.roster_digest,
            "updated_at": statement.excluded.updated_at,
        },
    ).returning(table.c.generation)
    return int(session.execute(statement).scalar_one())


def _replace_selected_profile_roster(
    session: Session,
    *,
    expected_generation: int,
    expected_application_id: str,
    expected_roster_digest: str,
    profile_id: str,
    profile_revision: int,
    application_id: str,
    node_ids: Sequence[str],
    now: datetime,
) -> int:
    """Advance a selected profile only if its intent and roster are unchanged."""

    result = session.execute(
        update(FleetProfileSelection)
        .where(
            FleetProfileSelection.singleton_id == 1,
            FleetProfileSelection.generation == expected_generation,
            FleetProfileSelection.application_id == expected_application_id,
            FleetProfileSelection.roster_digest == expected_roster_digest,
        )
        .values(
            generation=expected_generation + 1,
            profile_id=profile_id,
            profile_revision=profile_revision,
            application_id=application_id,
            roster_digest=_roster_digest(node_ids),
            updated_at=now,
        )
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        raise FleetProfileStalePlanConflict(
            "Selected profile or fleet membership changed during reconciliation"
        )
    return expected_generation + 1


def _replace_selected_profile_application(
    session: Session,
    *,
    expected_generation: int,
    expected_application_id: str,
    expected_roster_digest: str,
    application_id: str,
    now: datetime,
) -> None:
    """Point the current intent at a retry receipt without changing its intent."""

    result = session.execute(
        update(FleetProfileSelection)
        .where(
            FleetProfileSelection.singleton_id == 1,
            FleetProfileSelection.generation == expected_generation,
            FleetProfileSelection.application_id == expected_application_id,
            FleetProfileSelection.roster_digest == expected_roster_digest,
        )
        .values(application_id=application_id, updated_at=now)
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        raise FleetProfileStalePlanConflict(
            "Selected profile changed before its retry was admitted"
        )


def _switch_queue(
    stops: Sequence[FleetProfileRunEffect],
    work: Sequence[FleetProfileAssignment],
    removals: Sequence[str],
) -> list[FleetProfileSwitchQueueItem]:
    run_groups = [
        {node.node_id for node in assignment.nodes}
        for assignment in work
        if assignment.desired_state == DesiredAssignmentState.RUNNING
    ]
    replaced = [
        effect
        for effect in stops
        if any(set(effect.node_ids) <= group for group in run_groups)
    ]

    def stop(effect: FleetProfileRunEffect) -> FleetProfileSwitchQueueItem:
        return FleetProfileSwitchQueueItem(
            kind=ProfileChildPhase.STOP.value,
            id=effect.run_id,
            profile_stop_scope=effect.profile_stop_scope,
        )

    return [
        *(stop(effect) for effect in stops if effect not in replaced),
        *(
            FleetProfileSwitchQueueItem(
                kind=ProfileSwitchChildKind.INSTALL.value
                if assignment.desired_state == DesiredAssignmentState.INSTALLED
                else ProfileSwitchChildKind.RUN.value,
                id=assignment.id,
            )
            for assignment in work
        ),
        *(stop(effect) for effect in replaced),
        *(
            FleetProfileSwitchQueueItem(
                kind=ProfileChildPhase.CLEANUP.value, id=identity
            )
            for identity in removals
        ),
    ]


def _validate_remaining_effects(
    reviewed: FleetProfileEffects, remaining: FleetProfileEffects
) -> None:
    """Recovery may finish destructive effects, never acquire new targets."""
    reviewed_stops = {
        _digest(effect)
        for effect in reviewed.runs
        if effect.action == ProfileChildPhase.STOP.value
    }
    reviewed_removals = {
        _digest(effect)
        for effect in reviewed.installations
        if effect.action == "remove"
    }
    if any(
        effect.action == ProfileChildPhase.STOP.value
        and _digest(effect) not in reviewed_stops
        for effect in remaining.runs
    ):
        raise FleetProfileReviewStale(
            "Recovery would stop an unreviewed workload; review and load the current profile again"
        )
    if any(
        effect.action == "remove" and _digest(effect) not in reviewed_removals
        for effect in remaining.installations
    ):
        raise FleetProfileReviewStale(
            "Recovery would remove an unreviewed installation; review and load the current profile again"
        )


def _effective_option_choices(
    recipe: RecipeDefinition,
    stored: Mapping[str, str],
) -> tuple[dict[str, str], list[str]]:
    """The choice for every option the recipe declares, defaults filled in.

    Saving and reading are both tolerant: a newer recipe revision may no longer
    offer a stored option or value, and a profile holds one such choice per
    assignment it re-submits on every edit. A choice the recipe does not offer
    falls back to the default and the second result names what was replaced, so
    it is shown instead of blocking the profile.
    """

    try:
        return recipe.resolve_options(stored), []
    except RecipeOptionError:
        pass
    declared = {option.name: option for option in recipe.options}
    kept: dict[str, str] = {}
    notes: list[str] = []
    for name, value in stored.items():
        option = declared.get(name)
        if option is None:
            notes.append(f"Option {name} is no longer offered by the recipe")
        elif value not in {choice.value for choice in option.choices}:
            notes.append(
                f"Option {name}: {value} is no longer offered; using "
                f"{option.default_value}"
            )
        else:
            kept[name] = value
    return recipe.resolve_options(kept), notes


def _with_save_notes(view: FleetProfileView, notes: Sequence[str]) -> FleetProfileView:
    """Show what saving replaced, within the view's warning bound."""

    if not notes:
        return view
    warnings = [*notes, *view.warnings][:MAX_PROFILE_WARNINGS]
    return view.model_copy(update={"warnings": warnings})


def _choice_id(value: FleetProfileAssignmentInput) -> str:
    identity = ":".join(
        (
            value.recipe_selector,
            value.assignment_name or "",
            value.model_variant or "",
            value.desired_state,
            *value.spark_ids,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:fleet-profile:{identity}"))


def _expanded_roles(topology: RecipeTopology) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (role.name, role.endpoint_owner)
        for role in topology.roles
        for _ in range(role.count)
    )


def _profile_definition(row: FleetProfile) -> FleetProfileDefinition:
    """Read complete intent without embedding a damaged-column marker in a field."""

    labels = read_row_column(row, "labels")
    assignments = read_row_column(row, "assignments")
    for value in (labels, assignments):
        if isinstance(value, Residue):
            # Validate the unavailable document at the definition boundary. It
            # must fail there, even when its string fields could pass as labels.
            return FleetProfileDefinition.model_validate(value, strict=True)
    return FleetProfileDefinition.model_validate_json(
        canonical_message(
            {
                "name": row.name,
                "description": row.description,
                "installation_policy": row.installation_policy,
                "labels": labels,
                "favorite": row.favorite,
                "assignments": assignments,
            }
        ),
        strict=True,
    )


def _profile_document(row: FleetProfile) -> SavedProfileDocument:
    definition = _profile_definition(row)
    return SavedProfileDocument.model_validate_json(
        canonical_message(
            {
                "schema_version": 2,
                "id": row.id,
                "number": row.number,
                "revision": row.revision,
                **definition.model_dump(mode="json"),
            }
        )
    )


_assignment_id = _choice_id
