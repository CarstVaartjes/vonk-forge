"""Persistence for Fleet profiles."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_agent_protocol.agent_words import ProfileProjectionKind
from vonk_forge_contracts import RecipeDefinition

from .. import job_states
from ..fleet_profile_contract import (
    FLEET_PROFILE_ENDED_STATES,
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileEffects,
    FleetProfilePreview,
    FleetProfileScopePreview,
)
from ..job_documents import DistributionJobPayload, RunSwitchJobPayload, _RecipeParent
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..lifecycle.types import State as _LifecycleState
from ..models import CatalogDocumentRevision, FleetProfileApplication, Job
from ..stored_json import read_row_column
from ..strict_json import read_stored_document, stored_document_detail
from .activity import _profile_activity_state
from .contracts import (
    FleetProfileConflict,
)
from .projection_support import (
    _aware,
)


def _without_values[T](read: Callable[[], T]) -> Callable[[], T | Damaged]:
    """Make a reader's failure name the failing field, never the stored value.

    A pydantic error stringifies the offending value; the note of a residue is
    logged and may be shown, so only the field path and the error type survive.
    """

    def run() -> T | Damaged:
        try:
            return read()
        except ValidationError as error:
            return Damaged(
                stored_document_detail(error) or "stored document is invalid"
            )

    return run


def _rebuild_without_values[T](rebuild: Callable[[], T]) -> Callable[[], T | None]:
    """A rebuild that finds no evidence when the stored document is invalid."""

    def run() -> T | None:
        try:
            return rebuild()
        except ValidationError:
            return None

    return run


def _residue_detail(residue: Residue) -> str:
    """The reader's own description of why a stored document was retired."""

    return residue.note.split(": ", 1)[-1]


def _persisted_profile_plan(
    row: FleetProfileApplication,
) -> FleetProfilePreview | Residue:
    """Load the complete stored preview through its canonical contract.

    A plan that does not parse, or whose identity disagrees with its row, cannot
    be re-derived here (the reviewed decision is the only copy of what was
    consented to), so it is retired as unknown: the caller skips or retires that
    row and carries on.
    """

    def read() -> FleetProfilePreview | Damaged:
        plan = read_row_column(row, "plan")
        if isinstance(plan, Residue):
            return Damaged(_residue_detail(plan))
        if not isinstance(plan, FleetProfilePreview):
            return Damaged("the stored application has no plan")
        if (
            plan.profile_id != row.profile_id
            or plan.profile_digest != row.profile_digest
            or plan.plan_digest != row.plan_digest
        ):
            return Damaged("stored plan identity differs from its row")
        return plan

    return read_or_rebuild(
        kind="profile-plan",
        subject=str(row.id),
        read=_without_values(read),
        reason=BookkeepingReason.PERSISTED_STATE_DAMAGED,
    )


def _profile_application_effect_nodes(plan: FleetProfilePreview) -> tuple[str, ...]:
    """Return the exact sorted node scope of reviewed profile effects."""

    return tuple(
        sorted(
            {node_id for step in plan.steps for node_id in step.node_ids}
            | {
                node_id
                for effect in plan.effects.adopted
                for node_id in effect.node_ids
            }
        )
    )


def _application_effect_scope(row: FleetProfileApplication) -> tuple[str, ...]:
    """The nodes an application's effects can touch (its declared scope if unreadable)."""

    plan = _persisted_profile_plan(row)
    if isinstance(plan, Residue):
        return tuple(sorted(_persisted_profile_scope(row) or ()))
    return _profile_application_effect_nodes(plan)


def _persisted_profile_result(
    row: FleetProfileApplication,
) -> FleetProfileApplicationResult | None:
    """Load a stored result, re-deriving it from the receipt when it is damaged.

    The result of a load is a function of how far it got: the step it reached is
    on the row.  A succeeded row with a missing or damaged result is rebuilt from
    that evidence; a damaged result of any other row is retired (``None``).
    """

    def read() -> FleetProfileApplicationResult | Damaged | None:
        if row.result is None:
            if row.state == _LifecycleState.SUCCEEDED:
                return Damaged("a succeeded application has no result")
            return None
        result = read_row_column(row, "result")
        return (
            Damaged(_residue_detail(result)) if isinstance(result, Residue) else result
        )

    def rebuild() -> FleetProfileApplicationResult | None:
        if row.state != _LifecycleState.SUCCEEDED:
            return None
        # A succeeded load completed every step its plan listed; a plan that
        # cannot be read leaves the step the row itself reached.
        plan = _persisted_profile_plan(row)
        reached = len(plan.steps) if not isinstance(plan, Residue) else row.current_step
        steps = max(0, min(int(reached or 0), 1024))
        return FleetProfileApplicationResult(changed=steps > 0, completed_steps=steps)

    value = read_or_rebuild(
        kind="profile-result",
        subject=str(row.id),
        read=_without_values(read),
        rebuild=rebuild,
    )
    return None if isinstance(value, Residue) else value


def _canonical_progress(value: object) -> FleetProfileApplicationProgress:
    """Validate a persisted progress document with canonical JSON semantics.

    The document was written as JSON: nested contract tuples arrive as arrays
    and unions must resolve the way they did on the producer side.  Validating
    an already-decoded mapping as strict Python data instead lets Pydantic's
    smart union pick a different variant, which silently replaces a child
    receipt with a same-shaped neighbour.
    """

    return read_stored_document(
        lambda document: FleetProfileApplicationProgress.model_validate_json(
            canonical_message(document), strict=True
        ),
        value,
    )


def _read_profile_progress(
    row: FleetProfileApplication,
) -> FleetProfileApplicationProgress | Damaged:
    progress = read_row_column(row, "progress")
    if isinstance(progress, FleetProfileApplicationProgress):
        return progress
    return Damaged(
        _residue_detail(progress)
        if isinstance(progress, Residue)
        else "the stored application has no progress"
    )


def _persisted_profile_progress(
    row: FleetProfileApplication,
) -> FleetProfileApplicationProgress:
    """Load progress through its canonical contract before worker mutation.

    Damaged progress is rebuilt from the receipt the row itself carries (the step
    it reached, the child it issued).  The rebuilt progress names no accepted
    intent, so the worker retires the row as superseded with its effect unknown
    (the children keep their own lifecycle) instead of stopping on it.
    """

    value = read_or_rebuild(
        kind="profile-progress",
        subject=str(row.id),
        read=lambda: _read_profile_progress(row),
        rebuild=lambda: _progress_from_receipt(row),
    )
    # (The receipt always rebuilds it; the fallback keeps a defect from stopping a tick.)
    return _progress_from_receipt(row) if isinstance(value, Residue) else value


def _stored_progress(
    row: FleetProfileApplication,
) -> FleetProfileApplicationProgress | Residue:
    """The progress document as stored, or the residue of a damaged one.

    The worker uses this where *knowing* that the document is damaged decides
    what happens (a cancel in flight still completes, a load whose child record
    is lost is retired).  Everything that only reads progress uses
    :func:`_persisted_profile_progress`, which rebuilds it.
    """

    return read_or_rebuild(
        kind="profile-progress",
        subject=str(row.id),
        read=lambda: _read_profile_progress(row),
    )


def _progress_from_receipt(
    row: FleetProfileApplication,
) -> FleetProfileApplicationProgress:
    """The progress a row's own columns can prove (no intent, no child document)."""

    completed = max(0, min(int(row.current_step or 0), 1024))
    return FleetProfileApplicationProgress(
        completed_steps=completed, total_steps=completed
    )


def _owns_pending_admission(
    row: FleetProfileApplication, progress: FleetProfileApplicationProgress
) -> bool:
    """A late admission observer cannot replace a newer lifecycle decision."""
    return (
        row.state
        in job_states.words(LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR)
        and progress.admission_pending
        and progress.cancellation is None
    )


def _persisted_profile_scope(row: FleetProfileApplication) -> tuple[str, ...] | None:
    """Read the frozen scope through its authoritative stored plan contract.

    An unreadable plan has unknown scope; partial JSON cannot invent a narrower
    cleanup authority.
    """

    plan = read_row_column(row, "plan")
    if isinstance(plan, FleetProfilePreview):
        return tuple(plan.scope.node_ids)
    # A damaged step list does not erase the independently typed frozen scope.
    # This projection never reconstructs or authorizes the unknown step effects.
    if not isinstance(row.plan, dict):
        return None
    try:
        scope = FleetProfileScopePreview.model_validate_json(
            canonical_message(row.plan.get("scope")), strict=True
        )
    except (TypeError, ValueError):
        return None
    return tuple(scope.node_ids)


def _next_profile_acceptance_time(session: Session, now: datetime) -> datetime:
    """Order sequential receipts; concurrent inserts may share time and tie by ID."""

    latest = session.scalar(select(func.max(FleetProfileApplication.created_at)))
    if latest is None:
        return _aware(now)
    try:
        next_order = _aware(latest) + timedelta(microseconds=1)
    except OverflowError:
        # A stored time at the end of the supported range cannot be ordered
        # after: the receipt is ordered by the clock and breaks ties by ID.
        retire_as_unknown(
            "profile-order",
            "created_at",
            BookkeepingReason.EVIDENCE_MISMATCH,
            "latest acceptance time exceeds the supported range",
        )
        return _aware(now)
    return max(_aware(now), next_order)


def _application_order_key(
    session: Session,
    row: FleetProfileApplication,
) -> tuple[datetime, str]:
    """Order a receipt by its root reviewed intent, including its retries."""

    own_order = (_aware(row.created_at), row.id)
    progress = _persisted_profile_progress(row)
    intended = progress.intended_profile
    if intended is None:
        return own_order
    if intended.reviewed_application_id == row.id:
        if progress.retry_of_application_id is not None:
            retire_as_unknown(
                "profile-order",
                str(row.id),
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the reviewed lineage cannot establish an earlier acceptance",
            )
            return own_order
        return own_order
    root = session.get(FleetProfileApplication, intended.reviewed_application_id)
    if root is None:
        retire_as_unknown(
            "profile-order",
            str(row.id),
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
            "the reviewed lineage cannot establish an earlier acceptance",
        )
        return own_order
    root_progress = _persisted_profile_progress(root)
    if (
        root.profile_id != row.profile_id
        or root.profile_digest != row.profile_digest
        or root_progress.retry_of_application_id is not None
        or root_progress.intended_profile != intended
        or intended.reviewed_application_id != root.id
    ):
        retire_as_unknown(
            "profile-order",
            str(row.id),
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
            "the reviewed lineage cannot establish an earlier acceptance",
        )
        return own_order
    return _aware(root.created_at), root.id


def _newer_profile_intent_overlaps(
    session: Session,
    intent_order: tuple[datetime, str],
    effect_nodes: set[str],
) -> bool:
    """Whether a later durable profile receipt still owns overlapping intent.

    ``intended_profile`` is written when the request is accepted and retained
    through terminal states and retries. A later accepted receipt therefore
    remains authoritative even if it has already fenced nodes and then failed
    or was cancelled. Only overlapping execution steps participate.
    """

    if not effect_nodes:
        return False
    order = intent_order
    for candidate in session.scalars(
        select(FleetProfileApplication)
        .where(FleetProfileApplication.created_at >= order[0])
        .order_by(
            FleetProfileApplication.created_at,
            FleetProfileApplication.id,
        )
    ):
        try:
            progress = _persisted_profile_progress(candidate)
            candidate_order = _application_order_key(session, candidate)
        except FleetProfileConflict:
            continue
        if progress.admission_pending or progress.intended_profile is None:
            continue
        if candidate_order <= order:
            continue
        plan = _persisted_profile_plan(candidate)
        if isinstance(plan, Residue):
            persisted_scope = _persisted_profile_scope(candidate)
            if persisted_scope is None:
                continue
            candidate_nodes = set(persisted_scope)
        else:
            candidate_nodes = {
                node_id for step in plan.steps for node_id in step.node_ids
            }
        if candidate_nodes & effect_nodes:
            return True
    return False


def _stored_retry_lineage(row: FleetProfileApplication) -> str | None:
    """Malformed unrelated history cannot deny a valid receipt its recovery."""
    progress = read_row_column(row, "progress")
    return (
        progress.retry_of_application_id
        if isinstance(progress, FleetProfileApplicationProgress)
        else None
    )


def _stored_recipe(row: CatalogDocumentRevision) -> RecipeDefinition:
    """Only the shared JSON-column reader reconstructs a stored recipe."""
    return RecipeDefinition.model_validate(read_row_column(row, "document"))


def _workload_intent_ordinal(row: Job) -> int | None:
    """Workload intent is shared by lifecycle, run-switch and transfer parents."""
    payload = read_row_column(row, "payload")
    return (
        payload.workload_intent_ordinal
        if isinstance(
            payload, (RunSwitchJobPayload, _RecipeParent, DistributionJobPayload)
        )
        else None
    )


def _remaining_reviewed_effects(
    session: Session, reviewed: FleetProfileEffects
) -> FleetProfileEffects:
    """An already ended bookkeeping target consumes its reviewed no-op effect.

    Only reviewed pending targets can disappear. Run, installation, adoption and
    node effects remain exact, so this cannot authorize a newly discovered effect.
    """
    pending = []
    for effect in reviewed.superseded:
        if effect.kind == ProfileProjectionKind.JOB.value:
            job = session.get(Job, effect.id)
            if job is not None and job.state not in job_states.words(
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
                LifecycleState.SUPERSEDED,
            ):
                pending.append(effect)
        else:
            application = session.get(FleetProfileApplication, effect.id)
            if application is not None:
                progress = _persisted_profile_progress(application)
                if (
                    _profile_activity_state(application.state, progress.cancellation)
                    not in FLEET_PROFILE_ENDED_STATES
                ):
                    pending.append(effect)
    return reviewed.model_copy(update={"superseded": pending})
