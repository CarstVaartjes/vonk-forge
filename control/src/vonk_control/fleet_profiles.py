"""PostgreSQL authority for saved Fleet profiles and live-versus-desired plans."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypedDict
from typing import cast as _typing_cast

from pydantic import ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import String, case, cast, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, object_session, sessionmaker
from vonk_agent_protocol import (
    DesiredAssignmentState,
    EndpointState,
    InstallationNodeState,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    ModelCacheBlockerCode,
    ObservedAssignmentState,
    OperationFailureCode,
    ProfileReasonCode,
    ReservationState,
    RouteState,
    RunState,
    RunSwitchCode,
    RuntimeImageCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    SupersedeCode,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
    input_state,
)
from vonk_forge_contracts import RecipeOptionError, read_recipe
from vonk_forge_contracts.recipe import RecipeTopology

from . import fleet_profile_states, job_states
from .admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    is_admission_contention,
    node_admission_key,
)
from .agent_jobs import AgentJobService
from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from .artifact_reference_scan import require_model_sets_open
from .auth import MUTATION_ROLES, Actor
from .bounded_json import integer, require_mapping, sequence
from .catalog_revision_contract import read_catalog_document
from .categorized_errors import (
    InvalidType,
    InvalidValue,
    MissingRecord,
)
from .cluster_mappings import mapping_option_choices
from .failure_classification import error_code, is_security_failure
from .fleet_profile_contract import (
    MAX_PROFILE_WARNINGS,
    FleetProfileAction,
    FleetProfileAdmissionDecision,
    FleetProfileApplicationCancellationIntent,
    FleetProfileApplicationCancellationView,
    FleetProfileApplicationEffect,
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileApplicationView,
    FleetProfileAssignment,
    FleetProfileAssignmentAssessment,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentPreparation,
    FleetProfileAssignmentPreview,
    FleetProfileAssignmentState,
    FleetProfileAssignmentView,
    FleetProfileChildOperation,
    FleetProfileChildPhase,
    FleetProfileChildProgress,
    FleetProfileChildResult,
    FleetProfileDefinition,
    FleetProfileDefinitionView,
    FleetProfileEffects,
    FleetProfileEndpointAssignmentIntent,
    FleetProfileEndpointIntent,
    FleetProfileEndpointProjectionIssue,
    FleetProfileInput,
    FleetProfileInstallationEffect,
    FleetProfileInstallationPolicy,
    FleetProfileIntendedConfiguration,
    FleetProfileList,
    FleetProfileNode,
    FleetProfileOperationKind,
    FleetProfileOperationState,
    FleetProfilePendingEffect,
    FleetProfilePlanStep,
    FleetProfilePlanStepKind,
    FleetProfilePlanSummary,
    FleetProfilePreparationDecision,
    FleetProfilePreview,
    FleetProfileReason,
    FleetProfileReviewedDecision,
    FleetProfileRunEffect,
    FleetProfileScope,
    FleetProfileScopePreview,
    FleetProfileStepResult,
    FleetProfileSupersedeCode,
    FleetProfileSwitchAdapter,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileView,
    profile_switch_child_request_key,
)
from .lifecycle.core import RECOVERY
from .lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from .lifecycle.fleet_profile import (
    LEGACY_SUPERSEDED_PREFIXES,
    FleetProfileAdapter,
    cancellation_state,
    doc_state,
)
from .lifecycle.types import Effect as _LifecycleEffect
from .lifecycle.types import State as _LifecycleState
from .logging import redact_text
from .models import (
    STOPPABLE_RUN_STATES,
    AgentNode,
    AgentNodeProfile,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    ModelCacheSet,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
    User,
)
from .operation_blockers import (
    PHASE_RETRY_CODE,
    STALL_RETRY_ATTEMPT,
    OperationBlocker,
    bound_blockers,
    make_blocker,
)
from .operation_contract import OperationFailureEvidence
from .operation_progress import project_progress
from .preparation_contract import RolloutPreparation, RuntimeImageIdentity
from .profile_capacity import (
    release_unassigned_profile_claims,
    reserve_profile_disk,
    reserve_profile_memory,
    reserve_profile_ports,
    restore_released_profile_claims,
)
from .recipe_build_cancellation import (
    BuildConsumerError,
    lock_profile_build_dependencies,
)
from .recipe_execution_contract import (
    installation_matches_runtime_image,
    installation_serves_authorised_ports,
)
from .recipe_operations import RecipeOperationConflict
from .recipe_runtime_specs import (
    recipe_topology,
    resolve_recipe_entities,
)
from .recipe_update_notice import RecipeUpdateNotice, recipe_update_notice
from .revision_images import revision_archives
from .run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchAssessment,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPlacementAction,
    RunSwitchPlan,
    RunSwitchProfileStopScope,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from .run_switch_operations import (
    RunSwitchOperationConflict,
    RunSwitchOperationService,
)
from .settings import STORAGE_ADMISSION_RETRY_SECONDS, STORAGE_ADMISSION_WAIT_SECONDS
from .storage_demands import (
    STORAGE_EVICTING,
    STORAGE_EVICTION_TIMED_OUT,
    STORAGE_INSUFFICIENT,
    StorageRelief,
)
from .strict_json import (
    read_stored_document,
    read_stored_model,
    stored_document_detail,
    warn_unreadable_once,
)
from .user_authority import serialize_user_authority

if TYPE_CHECKING:
    from .operation_api import OperationProviderProtocol

_STORED_ASSIGNMENTS = TypeAdapter(
    list[FleetProfileAssignmentInput], config=ConfigDict(strict=True)
)
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_ACTIVE_INSTALL_STATES = frozenset(
    {
        InstallationState.PLANNED,
        InstallationState.INSTALLING,
        InstallationState.INSTALLED,
        InstallationState.PARTIAL,
        InstallationState.FAILED,
    }
)
# A child that waits for an operator (a legacy Run/Switch state, healed by its own
# tick) is still in progress: the application mirrors it as running and observes
# it again, never as a wait of its own.
_CHILD_PENDING_STATES = frozenset(
    {
        "queued",
        "pending",
        "leased",
        RunState.RUNNING,
        RunState.STARTING,
        RunState.STOPPING,
        InstallationState.INSTALLING,
        *job_states.words(LifecycleState.NEEDS_OPERATOR),
    }
)
_CHILD_FAILED_STATES = frozenset(
    job_states.words(LifecycleState.FAILED, LifecycleState.CANCELLED)
)
_PROFILE_ACTIVITY_ACTIVE_STATES = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
)
# Decoded and database-sourced closed values are read back through the
# contract's own alias, so a malformed state fails instead of reaching a typed
# model as an unvalidated string.
_OPERATION_STATE_ADAPTER = TypeAdapter(FleetProfileOperationState)
# Human labels for the loaded application's live assignment state.  "Running"
# is reported only once the run is up on every member and its route published.
_OBSERVED_ASSIGNMENT_LABELS: Mapping[FleetProfileAssignmentState, str] = {
    ObservedAssignmentState.NOT_PLACED: "Not placed",
    ObservedAssignmentState.PLACED: "Placed",
    ObservedAssignmentState.INSTALLING: "Installing",
    ObservedAssignmentState.INSTALLED: "Installed",
    ObservedAssignmentState.RUNNING: "Running",
    ObservedAssignmentState.DEGRADED: "Degraded",
}
_PROFILE_PHASE_ADAPTER = TypeAdapter(FleetProfileChildPhase)
_INSTALLATION_POLICY_ADAPTER = TypeAdapter(FleetProfileInstallationPolicy)
# Integrity and storage-access refusals of a child are not replayed blindly by
# profile recovery; receipts that do not validate would repeat child effects.
_PROFILE_RECOVERY_REFUSED_CODES = frozenset(
    {
        RunSwitchCode.RECEIPT_INVALID,
        RuntimeImageCode.ARCHIVE_UNAVAILABLE,
        RuntimeImageCode.RECEIPT_IDENTITY_CONFLICT,
        RuntimeImageCode.RECEIPT_IDENTITY_INVALID,
        RuntimeImageCode.RECEIPT_INVALID,
    }
)
#: The typed blocker a load ends with when the same failure repeated: waiting or
#: retrying cannot change a deterministic outcome (a start that crashes the same
#: way every time), so the Controller stops after ``RECOVERY.max_failures``.
PROFILE_REPEATED_FAILURE_CODE = ProfileReasonCode.FAILURE_REPEATED
_VARIABLE_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\b[0-9a-f]{12,}\b|\d+"
)
# A profile request may race a short heartbeat, telemetry write, or worker
# transaction while taking the reviewed admission snapshot.  Retry the whole
# SQL transaction after releasing it; the request key keeps a later successful
# attempt idempotent.  A persistent owner still reaches the normal busy error.
_PROFILE_ADMISSION_RETRY_DELAYS_SECONDS = (0.05, 0.15, 0.35)
#: How many parked applications one worker tick observes for a terminal child.
#: Bounded so a large parked backlog cannot turn one tick into an unbounded
#: scan, while still letting every parked order record its own ending.
_MAX_PARKED_APPLICATION_OBSERVATIONS = 8
_CANCELLATION_OBSERVATION_SECONDS = 5

_LOGGER = logging.getLogger(__name__)


def _profile_activity_pending(state, cancellation_state):
    """One pending predicate for in-memory display and SQL selection."""

    active = (
        state in _PROFILE_ACTIVITY_ACTIVE_STATES
        if isinstance(state, str)
        else state.in_(_PROFILE_ACTIVITY_ACTIVE_STATES)
    )
    if isinstance(cancellation_state, str) or cancellation_state is None:
        return active & fleet_profile_states.cancel_in_flight(cancellation_state)
    return active & cancellation_state.in_(fleet_profile_states.CANCEL_IN_FLIGHT)


def _profile_activity_state(
    state: str, cancellation: FleetProfileApplicationCancellationIntent | None
) -> str:
    """Project an active application from its durable cancellation owner."""

    if _profile_activity_pending(
        state, cancellation.state if cancellation is not None else None
    ):
        return fleet_profile_states.OBSERVING
    return state


def _profile_activity_state_expression():
    return case(
        (
            _typing_cast(
                Any,
                _profile_activity_pending(
                    FleetProfileApplication.state,
                    FleetProfileApplication.progress["cancellation"][
                        "state"
                    ].as_string(),
                ),
            ),
            fleet_profile_states.OBSERVING,
        ),
        # A failed application with a scheduled retry is presented as queued.
        (
            (FleetProfileApplication.state == "failed")
            & FleetProfileApplication.progress["retry_due_at"].as_string().is_not(None),
            "queued",
        ),
        else_=FleetProfileApplication.state,
    )


class PreparationStarter(Protocol):
    """Start (or find) the durable preparation of one exact recipe revision.

    The Controller prepares what a load needs by itself: the model download and
    the runtime image build.  The starter is idempotent for one revision and
    returns the reasons the preparation is not finished yet, so the load can
    say what it is waiting for.
    """

    def __call__(
        self, recipe_revision_id: str, *, actor: str
    ) -> Sequence[OperationBlocker]: ...


class StorageReliefProvider(Protocol):
    """Ask for free disk on one Spark for a load that was refused for lack of it.

    Returns the named reason to show on the waiting load, or ``None`` when the
    Spark's free space cannot be read or already covers the request.
    """

    def __call__(
        self,
        node_id: str,
        required_free_bytes: int,
        *,
        source: str,
        subject: str,
        reason: str,
    ) -> StorageRelief | None: ...


class PreparationCanceller(Protocol):
    """Cancel the pending preparation one exact recipe revision still runs.

    Returns the ids of the operations it asked to cancel. A preparation another
    accepted consumer still needs is left alone by its owner.
    """

    def __call__(
        self, recipe_revision_id: str, *, actor: str, reason: str
    ) -> Sequence[str]: ...


class _AssessmentProvider(Protocol):
    def __call__(
        self,
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
        /,
        *,
        allow_pending_cache_rebuild: bool,
        expected_runtime_image: RuntimeImageIdentity | None,
        excluded_profile_application_ids: tuple[str, ...],
    ) -> RunSwitchAssessment | Residue: ...


@dataclass(frozen=True)
class _ProfileControlEffects:
    """One SQL-owned reconciliation projection for review and admission."""

    states: dict[str, FleetProfileService._AssignmentState]
    effects: FleetProfileEffects
    changed_nodes: set[str]
    unavailable_assignment_ids: set[str]
    switch_needed: bool
    reasons: list[FleetProfileReason]


@dataclass(frozen=True)
class _SelectedProfileSnapshot:
    generation: int
    profile_id: str
    profile_revision: int
    application_id: str
    roster_node_ids: tuple[str, ...]
    roster_digest: str
    actor: str
    intended: FleetProfileIntendedConfiguration
    plan: FleetProfilePreview


class _PlanStepDraftRequired(TypedDict):
    """Required keys of one plan step before its index is assigned."""

    kind: FleetProfilePlanStepKind
    label: str


class _PlanStepDraft(_PlanStepDraftRequired, total=False):
    """Optional keys of one plan step before its index is assigned."""

    node_ids: list[str]


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
        plan = read_stored_document(
            lambda value: FleetProfilePreview.model_validate_json(
                json.dumps(value), strict=True
            ),
            row.plan,
        )
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

    return tuple(sorted({node_id for step in plan.steps for node_id in step.node_ids}))


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
        return read_stored_document(
            lambda value: FleetProfileApplicationResult.model_validate_json(
                json.dumps(value), strict=True
            ),
            row.result,
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
        read=_without_values(lambda: _canonical_progress(row.progress)),
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
        read=_without_values(lambda: _canonical_progress(row.progress)),
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
    """Read an application's declared frozen scope without decoding its plan.

    The scope written at admission is the durable authority for which nodes a
    pending order can still affect.  Its step list is a finer effect
    projection; when that projection is unreadable the scope is the only safe
    boundary, because a damaged document must never be narrowed into a guessed
    cleanup scope.  ``None`` means the stored scope itself cannot be trusted.
    """

    plan = row.plan
    if not isinstance(plan, Mapping):
        return None
    scope = plan.get("scope")
    if not isinstance(scope, Mapping):
        return None
    node_ids = scope.get("node_ids")
    if not isinstance(node_ids, Sequence) or isinstance(node_ids, (str, bytes)):
        return None
    if not all(
        isinstance(node_id, str) and _NODE_ID.fullmatch(node_id) is not None
        for node_id in node_ids
    ):
        return None
    return tuple(node_ids)


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
            raise FleetProfileUnavailable(
                "Persisted retry receipt cannot be its own reviewed intent",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return own_order
    root = session.get(FleetProfileApplication, intended.reviewed_application_id)
    if root is None:
        raise FleetProfileUnavailable(
            "Persisted application review source is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    root_progress = _persisted_profile_progress(root)
    if (
        root.profile_id != row.profile_id
        or root.profile_digest != row.profile_digest
        or root_progress.retry_of_application_id is not None
        or root_progress.intended_profile != intended
        or intended.reviewed_application_id != root.id
    ):
        raise FleetProfileUnavailable(
            "Persisted application review source is inconsistent",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
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


def _normalized_failure_text(value: object) -> str:
    """A failure reason with its changing parts (ids, counts, times) removed."""

    return " ".join(_VARIABLE_TEXT.sub("#", str(value or "")).split())[:240]


def _stored_retry_lineage(value: object) -> str | None:
    """Read only the explicit retry lineage one stored receipt declared.

    The scoped accepted-order scan handles newer overlapping receipts. This
    final profile-level check only needs explicit child lineage, so malformed
    unrelated history cannot deny a valid receipt its retry.
    """

    if not isinstance(value, Mapping):
        return None
    candidate = value.get("retry_of_application_id")
    return candidate if isinstance(candidate, str) else None


_PROFILE_PHASE_BY_RUN_PHASE = {
    "transfer": "target-copy",
    "verify": "final-verify",
    "prepare": "runtime-install",
    "final_verify": "final-verify",
}


#: A conflict that only time can resolve (a lock, capacity, a pending repair): the
#: application parks and the Controller retries it with backoff.
RETRY_WAIT = "wait"
#: A conflict whose inputs do not change by waiting (a stale review, a lost or moved
#: selection, a superseded intent): the application ends ``superseded`` so the
#: client reviews and submits again.
RETRY_SUPERSEDE = "supersede"


class FleetProfileConflict(RuntimeError):
    """A Fleet profile is invalid, stale, or cannot be safely applied.

    ``retry_disposition`` says what an automatic retry does with it, and every
    subclass declares it: ``RETRY_WAIT`` parks for retry, ``RETRY_SUPERSEDE``
    (the default for an untyped conflict, which is a state check that waiting
    cannot change) ends the application ``superseded`` with ``supersede_code``.
    """

    retry_disposition: ClassVar[str] = RETRY_SUPERSEDE
    supersede_code: ClassVar[FleetProfileSupersedeCode] = (
        SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION
    )


def retry_disposition_of(error: BaseException) -> str | None:
    """What an automatic retry does with ``error``; None for a non-conflict."""

    return getattr(type(error), "retry_disposition", None)


class FleetProfileInvalid(InvalidRequestError, FleetProfileConflict):
    """The request, or what it names, is out of contract or conflicts with the
    stored profile state; the caller corrects it and asks again."""

    retry_disposition = RETRY_SUPERSEDE


class FleetProfileUnavailable(UnknownOutcomeError, FleetProfileConflict):
    """Persisted bookkeeping or evidence that cannot settle the outcome here."""

    retry_disposition = RETRY_SUPERSEDE


class FleetProfileUnsupportedStore(InvalidRequestError, RuntimeError):
    """The database dialect cannot hold Fleet profile selection."""


class FleetProfileChildPlanBlocked(UnknownOutcomeError, RunSwitchOperationConflict):
    """The profile child's plan is blocked for now: observed again, not parked."""

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class FleetProfileAdmissionBusy(UnknownOutcomeError, FleetProfileConflict):
    """A transient admission owner must finish before the plan can be bound.

    ``holder`` names the kind of work that holds the Spark's admission lock when
    that is known, so the wait says what it is waiting for.
    """

    code = ProfileReasonCode.ADMISSION_BUSY
    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        message: str,
        *,
        holder: str | None = None,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(message, reason=reason)
        self.holder = holder


class FleetProfileAdmissionStorageError(UnknownOutcomeError, FleetProfileConflict):
    """A persisted intent awaits correction of a database constraint failure."""

    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class FleetProfileAdmissionEffectBusy(UnknownOutcomeError, FleetProfileConflict):
    """A live effect owner must finish before a superseding plan can bind.

    ``shortfalls`` holds ``(node_id, free_bytes_needed)`` when the wait is for
    disk on named Sparks that eviction may free: the parked load then asks the
    storage collector for exactly that, so the wait ends by itself, or in a
    typed refusal when nothing more can be freed.
    """

    code = ProfileReasonCode.ADMISSION_EFFECT_BUSY
    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
        shortfalls: tuple[tuple[str, int], ...] = (),
    ) -> None:
        super().__init__(*args, reason=reason)
        self.shortfalls = shortfalls


class FleetProfileResourceRecheckUnavailable(FleetProfileAdmissionEffectBusy):
    """The resource recheck under the admission fence failed for a named cause."""

    code = ProfileReasonCode.RESOURCE_RECHECK_UNAVAILABLE
    retry_disposition = RETRY_WAIT


class FleetProfileStalePlanConflict(InvalidRequestError, FleetProfileConflict):
    """Admission refused because the caller's reviewed plan is no longer current."""

    code = ProfileReasonCode.STALE_PLAN
    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION

    def __init__(
        self,
        *args: object,
        reason: InvalidRequestReason = InvalidRequestReason.SUPERSEDED,
    ) -> None:
        super().__init__(*args, reason=reason)


class FleetProfileReviewStale(FleetProfileStalePlanConflict):
    """The reviewed effects differ from the current plan; nothing was accepted."""

    code = ProfileReasonCode.REVIEW_STALE
    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION


class _FleetProfileSupersededIntentConflict(FleetProfileStalePlanConflict):
    """A later accepted intent owns an overlapping workload effect scope."""

    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.SUPERSEDED_BY_INTENT


class FleetProfileSelectionLost(FleetProfileStalePlanConflict):
    """A retry has no current selection to continue (it moved or was replaced).

    Waiting cannot give it one back; the retry's parent no longer owns the
    selected profile, so adopting it would undo a newer load.
    """

    code = ProfileReasonCode.SELECTION_LOST
    retry_disposition = RETRY_SUPERSEDE
    supersede_code = SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION


class FleetProfileAssetReservationConflict(FleetProfileConflict):
    """A profile asset could not be reserved right now; a later attempt may."""

    code = ProfileReasonCode.ASSET_RESERVATION_UNAVAILABLE
    retry_disposition = RETRY_WAIT


class FleetProfilePermissionDenied(SecurityRefusalError, PermissionError):
    """Current user authority cannot authorize this profile request."""

    def __init__(
        self,
        *args: object,
        reason: SecurityRefusalReason = SecurityRefusalReason.PERMISSION_DENIED,
    ) -> None:
        super().__init__(*args, reason=reason)


class _FleetProfileRecoveryBindingConflict(UnknownOutcomeError, FleetProfileConflict):
    """Recovery cannot adopt the currently available artifact identity."""

    retry_disposition = RETRY_WAIT

    def __init__(
        self,
        *args: object,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


def _recovery_preparation_identity(preparation: RolloutPreparation) -> object:
    """Retain typed artifact identities, excluding observations and build provenance."""

    return preparation.model_dump(
        mode="json",
        exclude={
            "model": {"controller", "targets", "completeness"},
            "runtime_image": {"controller", "targets", "build_id"},
            "exceptions": {"__all__": {"state", "reason"}},
            "controller_ready": True,
            "targets_ready": True,
            "ready": True,
            "reasons": True,
        },
    )


def _require_recovery_preparation(
    assignment_id: str,
    expected: RolloutPreparation | None,
    observed: RolloutPreparation | None,
    *,
    first_binding: bool = False,
) -> None:
    if observed is None:
        image = expected.runtime_image if expected is not None else None
        exact = (
            f", image {image.image_digest}, archive {image.oci_layout_sha256}"
            if image is not None
            else ""
        )
        raise _FleetProfileRecoveryBindingConflict(
            f"{ProfileReasonCode.RECOVERY_CACHE_PENDING}: Prepare cache on the Controller for "
            f"assignment {assignment_id}{exact}"
            + ("; recovery retains these exact identities." if exact else ".")
        )
    if expected is None:
        # The accepted plan never bound an identity for this assignment (it was
        # still waiting for its preparation, or its record is gone): there is no
        # bound identity to be replaced, so the verified preparation observed now
        # is what is bound, and Run/Switch verifies that digest again at ingress
        # when its child starts.
        if not first_binding:
            retire_as_unknown(
                "profile-recovery-identity",
                assignment_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the accepted artifact identity is unavailable; binding the "
                "verified preparation observed now",
            )
        return
    if _recovery_preparation_identity(expected) != _recovery_preparation_identity(
        observed
    ):
        raise _FleetProfileRecoveryBindingConflict(
            f"{ProfileReasonCode.RECOVERY_ARTIFACT_CHANGED}: Prepared assets for assignment "
            f"{assignment_id} differ from its accepted model/image identity; "
            "restore the exact accepted assets or use an explicit new load "
            "to bind the replacement.",
            reason=WaitReason.RETAINED_IDENTITY_MISMATCH,
        )


def _preview_blocker_codes(preview: FleetProfilePreview) -> list[str]:
    return [reason.code for reason in preview.reasons] + [
        blocker.code
        for item in preview.assessments
        for blocker in item.assessment.blockers
    ]


def _preview_blockers(preview: FleetProfilePreview) -> list[OperationBlocker]:
    """The typed reasons a reviewed plan cannot be admitted yet, node ids included."""

    blockers = [
        make_blocker(reason.code, reason.detail, severity=reason.severity)
        for reason in preview.reasons
        if reason.severity != "info"
    ]
    for item in preview.assessments:
        blockers.extend(
            make_blocker(
                reason.code,
                reason.detail,
                severity="error" if reason.severity == "blocker" else reason.severity,
                node_ids=reason.node_ids,
            )
            for reason in item.assessment.blockers
        )
    return bound_blockers(blockers)


#: Blockers only the Controller's own preparation (model download, runtime image
#: build) resolves; a load that has just these waits instead of being refused.
_PREPARATION_RESOLVABLE_CODES = frozenset(
    {ProfileReasonCode.PREPARATION_UNAVAILABLE, RunSwitchCode.RECIPE_BUILD_UNAVAILABLE}
)


def _assignments_needing_preparation(
    assignments: Sequence[FleetProfileAssignmentPreview],
    prepared: set[str],
    reasons: Sequence[FleetProfileReason],
    assessments: Sequence[FleetProfileAssignmentAssessment],
) -> list[FleetProfileAssignmentPreview]:
    """Assignments that must place assets nobody has prepared yet.

    Anything the fleet or the recipe cannot resolve by itself (a Spark that is
    missing, an incomplete topology) never starts a preparation.
    """

    assessed_missing = {
        item.assignment_id
        for item in assessments
        if any(
            reason.code in _PREPARATION_RESOLVABLE_CODES
            for reason in item.assessment.blockers
        )
    }
    reason_missing = any(
        reason.code in _PREPARATION_RESOLVABLE_CODES for reason in reasons
    )
    return [
        assignment
        for assignment in assignments
        if "switch" in assignment.actions
        and not any(reason.severity == "error" for reason in assignment.reasons)
        and (
            assignment.assignment_id in assessed_missing
            or (reason_missing and assignment.assignment_id not in prepared)
        )
    ]


def _progress_with_blockers(
    progress: FleetProfileApplicationProgress,
    blockers: Sequence[OperationBlocker],
    **changes: object,
) -> dict[str, object]:
    """Canonical progress document carrying the application's current blockers."""

    data = progress.model_dump(mode="json")
    data["blockers"] = [
        item.model_dump(mode="json") for item in bound_blockers(blockers)
    ]
    data.update(changes)
    return read_stored_model(
        FleetProfileApplicationProgress,
        canonical_message(data),
        strict=True,
        from_json=True,
    ).model_dump(mode="json")


def _profile_preview_is_waitable(preview: FleetProfilePreview) -> bool:
    """Any blocker except a security boundary parks the intent for re-planning."""
    return not any(
        is_security_failure(code) for code in _preview_blocker_codes(preview)
    )


_DISK_REFUSALS = frozenset(
    {RunSwitchCode.INSUFFICIENT_DISK, RunSwitchCode.DISK_EVICTION_PLANNED}
)


def _disk_shortfalls(assessment: Any) -> tuple[tuple[str, int], ...]:
    """``(node_id, free_bytes_needed)`` for every Spark an assessment refuses
    (or plans an eviction) for lack of disk."""

    refused = {
        node_id
        for reason in (*assessment.blockers, *assessment.warnings)
        if reason.code in _DISK_REFUSALS
        for node_id in reason.node_ids
    }
    fit = assessment.fit_after_stop or assessment.fit_current
    return tuple(
        (node.node_id, node.disk_free_bytes - node.disk_free_after_bytes)
        for node in fit.nodes
        if node.node_id in refused
        and node.disk_free_bytes is not None
        and node.disk_free_after_bytes is not None
        and node.disk_free_after_bytes < 0
    )


_STORAGE_CODES = frozenset(
    {STORAGE_EVICTING, STORAGE_INSUFFICIENT, STORAGE_EVICTION_TIMED_OUT}
)


def _storage_wait_of_preview(
    preview: FleetProfilePreview,
) -> FleetProfileAdmissionEffectBusy | None:
    """The disk a blocked plan is waiting for, as a bounded storage wait."""

    shortfalls = tuple(
        shortfall
        for item in preview.assessments
        for shortfall in _disk_shortfalls(item.assessment)
    )
    if not shortfalls:
        return None
    return FleetProfileAdmissionEffectBusy(
        "Waiting for disk on "
        + ", ".join(sorted({node_id for node_id, _needed in shortfalls}))
        + ".",
        shortfalls=shortfalls,
    )


def _storage_wait_of(error: BaseException) -> FleetProfileAdmissionEffectBusy | None:
    if isinstance(error, FleetProfileAdmissionEffectBusy) and error.shortfalls:
        return error
    return None


def _relief_blocker(node_id: str, found: StorageRelief) -> OperationBlocker:
    return make_blocker(
        found.code,
        found.detail,
        severity="error" if found.code == STORAGE_INSUFFICIENT else "warning",
        node_ids=(node_id,),
    )


def _deferral_code(error: BaseException) -> str:
    """The blocker code a parked admission shows for the refusal that parked it."""

    if isinstance(error, FleetProfileResourceRecheckUnavailable):
        return error.code
    return ProfileReasonCode.ADMISSION_BUSY


def _error_summary(error: BaseException) -> str:
    """A short, safe description of an error: its type and the start of its text."""

    code = getattr(error, "code", None)
    name = code if isinstance(code, str) and code else type(error).__name__
    text = redact_text(" ".join(str(error).split()))[:160]
    return f"{name}: {text}" if text else name


def _require_recovery_preparations(
    accepted: FleetProfilePreview, current: FleetProfilePreview
) -> None:
    expected = {item.assignment_id: item.preparation for item in accepted.preparations}
    observed = {item.assignment_id: item.preparation for item in current.preparations}
    current_assignments = {item.assignment_id: item for item in current.assignments}
    for assignment in accepted.assignments:
        current_assignment = current_assignments.get(assignment.assignment_id)
        if (
            assignment.actions == ["keep"]
            and current_assignment is not None
            and current_assignment.actions == ["keep"]
        ):
            # Kept runtime state needs no cache preparation. A later run child
            # still checks its accepted identity before it can issue any work.
            continue
        _require_recovery_preparation(
            assignment.assignment_id,
            expected.get(assignment.assignment_id),
            observed.get(assignment.assignment_id),
            first_binding=not accepted.allowed,
        )


def _operation_state(
    value: object, *, default: FleetProfileOperationState
) -> FleetProfileOperationState:
    """Read one stored child operation state, keeping absence distinct.

    ``None`` is genuinely absent, so the caller's deliberate default stands.  A
    stored state outside the closed contract is unknown, and unknown is observed
    again (the caller's default is the observing state), never parked.
    """

    if value is None:
        return default
    try:
        return _OPERATION_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        retire_as_unknown(
            "profile-child-state",
            str(value)[:80],
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "stored child operation state is outside the contract",
        )
        return default


#: The operation state a retired load ends in (its effect recorded unknown).
_CANCELLED_OPERATION: FleetProfileOperationState = _typing_cast(
    "FleetProfileOperationState", _LifecycleState.CANCELLED.value
)


def _stored_state(state: _LifecycleState | None) -> str:
    """The stored label of a profile's aggregate state (``succeeded`` when empty)."""

    return "succeeded" if state is None else state.value


def _string_items(value: object, *, fallback: Sequence[str] = ()) -> list[str]:
    """Read a decoded JSON string array; damaged elements are dropped.

    A document that is not an array reads as ``fallback`` (the value derived from
    another receipt); an element that is not a string is skipped, never a reason
    to refuse the rest.
    """

    items = sequence(value)
    if items is None:
        if value is not None:
            retire_as_unknown(
                "profile-string-list",
                "state",
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "stored value is not an array",
            )
        return list(fallback)
    return [item for item in items if isinstance(item, str)]


class RunSwitchFleetProfileAdapter:
    """Compose one durable profile child from Run/Switch operations.

    The adapter plans conflicts against the complete profile assignment set
    before queuing any Run/Switch child.  Run/Switch remains the authority for
    each exact model, recipe revision, mapping, cache transfer, and run
    admission; this class only sequences those children under one profile
    application identity.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        run_switch: RunSwitchOperationService,
    ) -> None:
        self._sessions = sessions
        self._run_switch = run_switch

    def request_superseded_workload_cancellation_in_session(
        self,
        session: Session,
        targets: tuple[str, ...],
        ordinal: int,
        now: datetime,
    ) -> None:
        self._run_switch.request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )

    def request_cancellation(
        self,
        application_id: str,
        *,
        request_key: str,
        actor: str,
    ) -> None:
        """Forward the durable profile request to its exact active child.

        The read transaction ends before Run/Switch opens its own mutation
        transaction. The persisted parent cancellation is the retry authority,
        so a worker can safely repeat this call after a process restart.
        """

        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(application)
            cancellation = progress.cancellation
            state = self._state(application)
            active = None if state is None else state.get("active_operation_id")
            if (
                cancellation is None
                or cancellation.request_key != request_key
                or cancellation.actor != actor
                or not isinstance(active, str)
            ):
                return
            child_id = active
        try:
            self._run_switch.cancel(
                child_id,
                actor=actor,
                request_key=request_key,
                reason="Profile application cancellation",
            )
        except (KeyError, RunSwitchOperationConflict):
            # A child past its safe cancellation boundary or already
            # superseded remains owned by Run/Switch. The profile worker keeps
            # its dependency visible and reconciles the child's durable result.
            return

    def _failed_children(
        self, application_id: str, *, session: Session
    ) -> tuple[dict[str, object], list[RunSwitchOperation]] | None:
        """Return the adapter state and every child recorded as failed."""

        application = session.get(FleetProfileApplication, application_id)
        state = None if application is None else self._state(application)
        if state is None:
            return None
        failures = sequence(state.get("assignment_failures")) or ()
        operation_ids = {
            value
            for value in (
                state.get("active_operation_id"),
                *(
                    failure.get("operation_id")
                    for failure in failures
                    if isinstance(failure, Mapping)
                ),
            )
            if isinstance(value, str)
        }
        children: list[RunSwitchOperation] = []
        for operation_id in sorted(operation_ids):
            try:
                children.append(self._run_switch.get(operation_id))
            except (KeyError, RuntimeError, TypeError, ValueError):
                continue
        return state, children

    def recovery_refused(self, application_id: str, *, session: Session) -> bool:
        """A security or receipt-validation refusal is not replayed.

        A receipt that fails validation must not repeat the child's effects.
        Downloaded bytes that fail their digest are recovered: the replay
        discards them and downloads again.
        """

        found = self._failed_children(application_id, session=session)
        if found is None:
            return False
        state, children = found
        if any(
            isinstance(failure, Mapping) and failure.get("terminal") is True
            for failure in sequence(state.get("assignment_failures")) or ()
        ):
            return True
        for child in children:
            code = child.result.failure_code if child.result is not None else None
            if is_security_failure(code) or code in _PROFILE_RECOVERY_REFUSED_CODES:
                return True
        return False

    def failure_signature(self, application_id: str, *, session: Session) -> str | None:
        """What this application's children failed with, without identities.

        The failed child's typed code, phase and normalized reason (operation ids,
        counts and timings stripped) identify a deterministic failure: the same
        signature on consecutive attempts is the same fault.  None when the cause
        is unknown or can change by waiting (a Controller cache loss, a child that
        retries itself), which never counts toward the repeat budget.
        """

        found = self._failed_children(application_id, session=session)
        if found is None or self.recoverable_cache_loss(
            application_id, session=session
        ):
            return None
        state, children = found
        parts: set[str] = set()
        for child in children:
            if child.state not in job_states.words(LifecycleState.FAILED):
                continue
            result = child.result
            if result is not None and result.retryable:
                return None
            parts.add(
                "|".join(
                    (
                        (result.failure_code if result is not None else None) or "-",
                        str(
                            (result.failed_phase or result.phase)
                            if result is not None
                            else child.current_phase
                        ),
                        str(result.subphase if result is not None else None),
                        _normalized_failure_text(child.status_reason),
                    )
                )
            )
        if not parts:
            for failure in sequence(state.get("assignment_failures")) or ():
                if isinstance(failure, Mapping):
                    parts.add(
                        "recorded|" + _normalized_failure_text(failure.get("reason"))
                    )
        return "\n".join(sorted(parts)) or None

    def recoverable_cache_loss(self, application_id: str, *, session: Session) -> bool:
        """Recognize only a typed, pre-effect Controller cache loss."""

        found = self._failed_children(application_id, session=session)
        if found is None or found[0].get("state") != "failed":
            return False
        return any(
            child.state == "failed"
            and child.result is not None
            and child.result.phase == "prepare"
            and child.result.subphase == "runtime-image"
            and child.result.child_operation_id is None
            and child.result.failure_code == RuntimeImageCode.CACHE_MISSING
            for child in found[1]
        )

    def start(
        self,
        *,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation | Residue:
        scope = set(scope_node_ids)
        if tuple(scope_node_ids) != tuple(sorted(scope)):
            raise FleetProfileInvalid(
                "profile switch scope must be the complete sorted node boundary",
                reason=InvalidRequestReason.MALFORMED,
            )
        # An assignment that names a node outside the bound scope is not part of
        # this child: it is skipped (and logged), never allowed to widen the
        # scope the parent reviewed, and never a reason to refuse the others.
        ordered_assignments = tuple(
            sorted(
                (
                    assignment
                    for assignment in assignments
                    if all(node.node_id in scope for node in assignment.nodes)
                ),
                key=lambda item: item.id,
            )
        )
        if len(ordered_assignments) != len(assignments):
            retire_as_unknown(
                "profile-assignment",
                application_id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "an assignment names a node outside the bound scope",
            )
        with self._sessions.begin() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                return retire_as_unknown(
                    "profile-application",
                    application_id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "the application row is not stored",
                )
            existing = self._state(application)
            if (
                existing is None
                and _persisted_profile_progress(application).cancellation is not None
            ):
                raise FleetProfileInvalid(
                    "Profile cancellation prevents dispatch of another child",
                    reason=InvalidRequestReason.CONFLICT,
                )
            if existing is not None:
                # The child already started under this identity: it is adopted as
                # it was issued (an old run survives an upgrade), whatever scope a
                # replayed request names now.  Its own scope is the durable one.
                return self._view_from_state(application, existing)
            intended = FleetProfileService._intended_profile(
                application, session=session
            )
            plan = _persisted_profile_plan(application)
            reviewed_plan = FleetProfileService._reviewed_profile_plan(
                application, session=session
            )
            if isinstance(intended, Residue):
                return intended
            if isinstance(plan, Residue):
                return plan
            if isinstance(reviewed_plan, Residue):
                return reviewed_plan
            queue = self._plan_queue(
                session,
                ordered_assignments,
                scope_node_ids,
                application_id=application_id,
                installation_policy=intended.installation_policy,
                reviewed_effects=plan.effects,
                expected_images={
                    item.assignment_id: item.runtime_image
                    for item in reviewed_plan.preparation_decisions
                },
            )
            state: dict[str, object] = {
                "schema_version": 2,
                "child_id": application_id,
                "scope_node_ids": list(scope_node_ids),
                "assignment_ids": [item.id for item in ordered_assignments],
                "assignments": [
                    item.model_dump(mode="json") for item in ordered_assignments
                ],
                "queue": queue,
                "position": 0,
                "active_operation_id": None,
                "active_kind": None,
                "children": [],
                "actor": actor,
                "request_id": request_id,
            }
        self._save_state(application_id, state)
        return self._advance(application_id, ordered_assignments)

    def get(
        self, operation_id: str, *, session: Session | None = None
    ) -> FleetProfileChildOperation:
        """Observe the current child without ticking or dispatching work."""
        with (
            nullcontext(session) if session is not None else self._sessions()
        ) as current:
            application = current.get(FleetProfileApplication, operation_id)
            if application is None:
                raise MissingRecord(operation_id, reason=InvalidRequestReason.NOT_FOUND)
            state = self._state(application)
            if state is None:
                raise MissingRecord(operation_id, reason=InvalidRequestReason.NOT_FOUND)
            active = state.get("active_operation_id")
            child = self._observed_child(active) if isinstance(active, str) else None
            if child is not None:
                return self._view_from_child(operation_id, state, child)
            # No child, or one that is gone (recorded): the mirror the
            # application itself holds is what is shown, until a tick re-enters it.
            return self._view_from_state(application, state)

    def advance(
        self, operation_id: str, *, session: Session | None = None
    ) -> FleetProfileChildOperation:
        if session is not None:
            # The caller already holds this application's row. Joining its
            # transaction keeps the mirror write atomic with it, and the caller's
            # own loop ticks the run-switch coordinator, so no tick is run here.
            application = session.get(FleetProfileApplication, operation_id)
            if application is None:
                raise MissingRecord(operation_id, reason=InvalidRequestReason.NOT_FOUND)
            state = self._state(application)
            if state is None:
                raise MissingRecord(operation_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._advance(
                operation_id,
                self._assignments_from_state(state, application),
                session=session,
            )
        with self._sessions() as own:
            application = own.get(FleetProfileApplication, operation_id)
            if application is None:
                raise MissingRecord(operation_id, reason=InvalidRequestReason.NOT_FOUND)
            state = self._state(application)
            if state is None:
                raise MissingRecord(operation_id, reason=InvalidRequestReason.NOT_FOUND)
            assignments = self._assignments_from_state(state, application)
        return self._advance(operation_id, assignments)

    @staticmethod
    def _assignment_intent(
        assignment: FleetProfileAssignment,
    ) -> tuple[RunSwitchPlacementAction, str]:
        return (
            "install"
            if assignment.desired_state == DesiredAssignmentState.INSTALLED
            else "switch",
            assignment.alias or assignment.recipe_title.lower().replace(" ", "-"),
        )

    @staticmethod
    def _assignment_request(
        session: Session,
        assignment: FleetProfileAssignment,
        *,
        request_key: str | None = None,
    ) -> RunSwitchApplyRequest | Residue:
        """Map one profile assignment onto the Run/Switch authority.

        This is the single definition of how a profile assignment becomes a
        Run/Switch request.  Both apply and preparation projection use it, so a
        preview can never describe a different model, revision, group or alias
        than the child operation it later binds.
        """

        revision = session.get(CatalogDocumentRevision, assignment.recipe_revision_id)
        if revision is None:
            return retire_as_unknown(
                "profile-assignment",
                assignment.id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the recipe revision is not stored",
            )
        resolved = resolve_recipe_entities(session, revision.document)
        models = resolved.get("models")
        model_digest = (
            models[0].content_digest
            if isinstance(models, Sequence) and models
            else None
        )
        if not isinstance(model_digest, str):
            # The recipe revision this assignment names no longer resolves to an
            # exact model: unknown, not a verdict.  The caller leaves the
            # assignment out (the profile follows the newest revision on its next
            # reconciliation) instead of refusing the whole load.
            return retire_as_unknown(
                "profile-assignment",
                assignment.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the recipe revision resolves to no exact model identity",
            )
        group = SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    endpoint_owner=node.endpoint_owner,
                )
                for node in sorted(assignment.nodes, key=lambda item: item.rank)
            ]
        )
        action, alias = RunSwitchFleetProfileAdapter._assignment_intent(assignment)
        return RunSwitchApplyRequest(
            model_content_sha256=model_digest,
            recipe_revision_id=assignment.recipe_revision_id,
            spark_group=group,
            alias=alias,
            action=action,
            retention="retain-cached",
            option_choices=dict(assignment.option_choices),
            plan_digest=None,
            request_key=request_key,
        )

    def assess(
        self,
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
        *,
        allow_pending_cache_rebuild: bool = False,
        expected_runtime_image: RuntimeImageIdentity | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> RunSwitchAssessment | Residue:
        """Project admission and preparation from the same workload planner.

        Run/Switch remains the authority for the exact model artifact set, the
        runtime image identity and per-target readiness.  Asking it to plan is
        what keeps a profile preview honest: the preview shows the same
        preparation evidence the child operation will require, instead of a
        second, weaker estimate maintained at the profile boundary.
        """

        observed = tuple(sorted(node.node_id for node in assignment.nodes))
        if observed != expected_nodes:
            raise InvalidValue(
                "Profile assignment nodes changed during preparation projection.",
                reason=InvalidRequestReason.CONFLICT,
            )
        request = self._assignment_request(session, assignment)
        if isinstance(request, Residue):
            return request
        plan = self._run_switch.inspect_request(
            request,
            actor="controller:profile-preparation",
            defer_source_build=allow_pending_cache_rebuild,
            expected_runtime_image=expected_runtime_image,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )
        return plan.assessment()

    def validate_resources_in_session(
        self,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        reviewed: FleetProfilePreview,
    ) -> None:
        live_scope = set(reviewed.scope.node_ids)
        node_ids = tuple(
            sorted(
                {
                    node.node_id
                    for assignment in assignments
                    for node in assignment.nodes
                }
                & live_scope
            )
        )
        if node_ids:
            # Admission already holds the exact AgentNode rows. Lock the
            # mutable capacity facts those nodes reference so an inventory
            # refresh or reservation writer cannot change the fit between the
            # recheck and the claims committed below. Inserts are fenced by
            # the parent AgentNode row lock and do not require a table lock.
            tuple(
                session.scalars(
                    select(NodeInventorySnapshot)
                    .where(NodeInventorySnapshot.node_id.in_(node_ids))
                    .order_by(
                        NodeInventorySnapshot.node_id,
                        NodeInventorySnapshot.observed_at,
                        NodeInventorySnapshot.id,
                    )
                    .with_for_update(nowait=True)
                )
            )
            tuple(
                session.scalars(
                    select(ResourceReservation)
                    .where(
                        ResourceReservation.node_id.in_(node_ids),
                        ResourceReservation.state.in_(
                            (ReservationState.ACTIVE, ReservationState.PROMISED)
                        ),
                    )
                    .order_by(ResourceReservation.node_id, ResourceReservation.id)
                    .with_for_update(nowait=True)
                )
            )
        assessments = {item.assignment_id: item for item in reviewed.assessments}
        decisions = {item.assignment_id: item for item in reviewed.admission_decisions}
        changing = {
            item.assignment_id
            for item in reviewed.assignments
            if item.current_state != item.desired_state
        }
        for assignment in assignments:
            if (
                assignment.id not in changing
                or not {node.node_id for node in assignment.nodes} <= live_scope
            ):
                continue
            previous = assessments.get(assignment.id)
            decision = decisions.get(assignment.id)
            request = self._assignment_request(session, assignment)
            if previous is None or decision is None or isinstance(request, Residue):
                # No reviewed evidence (or no exact model) to recheck against: the
                # recheck is skipped for this assignment, and its Run/Switch child
                # admits it against current capacity itself when it starts.
                retire_as_unknown(
                    "profile-resource-recheck",
                    assignment.id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "no reviewed admission evidence to recheck",
                )
                continue
            try:
                fresh = self._run_switch.recheck_resources_in_session(
                    session,
                    request,
                    previous.assessment,
                    excluded_profile_application_ids=tuple(
                        item.id
                        for item in reviewed.effects.superseded
                        if item.kind == "profile-application"
                    ),
                )
            except (
                RunSwitchOperationConflict,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                # Name the cause: a failure that repeats on every retry is a
                # defect to see, not a wait to sit through.
                _LOGGER.warning(
                    "profile resource recheck for assignment %s failed: %s",
                    assignment.id,
                    _error_summary(error),
                    exc_info=True,
                )
                raise FleetProfileResourceRecheckUnavailable(
                    "Profile resource admission could not be rechecked "
                    f"({_error_summary(error)}); retrying automatically"
                ) from error
            current = FleetProfileAdmissionDecision.from_assessment(
                FleetProfileAssignmentAssessment(
                    assignment_id=assignment.id, assessment=fresh
                )
            )
            if not current.allowed:
                reasons = ", ".join(reason.code for reason in current.blockers[:4])
                raise FleetProfileAdmissionEffectBusy(
                    "Profile resource admission is waiting for capacity"
                    + (f": {reasons}" if reasons else ""),
                    shortfalls=_disk_shortfalls(fresh),
                )

    def _advance(
        self,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        *,
        session: Session | None = None,
    ) -> FleetProfileChildOperation:
        """Advance the switch child, joining a caller's transaction when given one.

        A caller that already holds the application row passes its session: a
        second transaction writing that row waits for the caller's own lock and
        deadlocks the worker.
        """

        if session is not None:
            return self._advance_in_session(session, application_id, assignments)
        with self._sessions.begin() as own:
            return self._advance_in_session(own, application_id, assignments)

    def _advance_in_session(
        self,
        session: Session,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
    ) -> FleetProfileChildOperation:

        application = session.get(FleetProfileApplication, application_id)
        if application is None:
            raise MissingRecord(application_id, reason=InvalidRequestReason.NOT_FOUND)
        state = self._state(application)
        if state is None:
            raise MissingRecord(application_id, reason=InvalidRequestReason.NOT_FOUND)
        cancelling = _persisted_profile_progress(application).cancellation is not None
        active = state.get("active_operation_id")
        position = state.get("position")
        queue_state = sequence(state.get("queue")) or ()
        current_item = (
            queue_state[integer(position) or 0]
            if (integer(position) or 0) < len(queue_state)
            else None
        )
        active_kind = state.get("active_kind") or (
            current_item.get("kind") if isinstance(current_item, Mapping) else None
        )
        expected_partial_failure = False
        child = self._observed_child(active) if isinstance(active, str) else None
        if isinstance(active, str) and child is None:
            # Bookkeeping, not a verdict: the child's record is gone (pruned, or
            # lost with a database restore).  Its identity is deterministic from
            # the application and the step, so the step is entered again and the
            # Run/Switch owner reconciles whatever that child already did, instead
            # of the load failing for it.
            _LOGGER.warning(
                "profile application %s: its Run/Switch child %s is gone; "
                "issuing the step again",
                application_id,
                active,
            )
            # (Nothing is flushed yet: the step is issued by its owner in its own
            # transaction below, and this write is committed with the new child.)
            state["active_operation_id"] = None
            state["active_kind"] = None
            self._write_state(session, application, state)
            active = None
        if isinstance(active, str) and child is not None:
            # ``waiting`` is an automatically observed Run/Switch state (for
            # example background runtime-image preparation or an overdue start
            # observation); the child resumes on its own, so it is in progress.
            # So is a legacy ``waiting-for-operator`` child (no Run/Switch action
            # exists; its own tick heals it): the profile mirrors it as running and
            # observes it again, never as a wait of its own.
            if child.state in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.OBSERVING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                view = self._view_from_child(application_id, state, child)
                new_progress = (
                    view.progress.model_dump(mode="json")
                    if view.progress is not None
                    else None
                )
                if (
                    state.get("state") != view.state
                    or state.get("child_progress") != new_progress
                ):
                    doc_state(state, view.state)
                    state["child_progress"] = new_progress
                    self._write_state(session, application, state)
                    session.flush()
                return view
            if child.state in {"failed", "cancelled"}:
                if cancelling:
                    children = list(sequence(state.get("children")) or ())
                    receipt = self._child_receipt(child)
                    children.append(
                        {
                            "operation_id": child.operation_id,
                            "kind": active_kind,
                            "state": child.state,
                            "result": (
                                receipt.model_dump(mode="json")
                                if receipt is not None
                                else None
                            ),
                        }
                    )
                    state["children"] = children
                    state["active_operation_id"] = None
                    state["active_kind"] = None
                    state["position"] = (integer(position) or 0) + 1
                    self._write_state(session, application, state)
                    session.flush()
                    pending_cancellation = self._observe_superseded_agent_effects(
                        session, application, state
                    )
                    if pending_cancellation is not None:
                        return pending_cancellation
                    return self._finish_cancelled_in_session(
                        session, application, state
                    )
                queue = sequence(state.get("queue")) or ()
                current_item = (
                    queue[integer(position) or 0]
                    if (integer(position) or 0) < len(queue)
                    else None
                )
                expected_partial_failure = (
                    child.state == "failed"
                    and state.get("active_kind") == "stop"
                    and isinstance(current_item, Mapping)
                    and isinstance(current_item.get("profile_stop_scope"), Mapping)
                    and child.result is not None
                    and child.result.failure_code
                    == RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL
                )
                if not expected_partial_failure:
                    reason = child.status_reason or (
                        f"Run/Switch child ended in {child.state}"
                    )
                    failures = list(sequence(state.get("assignment_failures")) or ())
                    failures.append(
                        {
                            "assignment_id": (
                                current_item.get("assignment_id")
                                if isinstance(current_item, Mapping)
                                else None
                            ),
                            "operation_id": child.operation_id,
                            "reason": reason[:512],
                            "terminal": is_security_failure(
                                child.result.failure_code
                                if child.result is not None
                                else None
                            ),
                        }
                    )
                    state["assignment_failures"] = failures
                    # The failure is recorded per assignment; the shared code
                    # below records the child and continues with the next one.
                    expected_partial_failure = True
                children = list(sequence(state.get("children")) or ())
                receipt = self._child_receipt(child)
                children.append(
                    {
                        "operation_id": child.operation_id,
                        "kind": active_kind,
                        "state": child.state,
                        "result": (
                            receipt.model_dump(mode="json")
                            if receipt is not None
                            else None
                        ),
                    }
                )
                state["children"] = children
                state["active_operation_id"] = None
                state["active_kind"] = None
                state["position"] = (integer(position) or 0) + 1
                self._write_state(session, application, state)
                session.flush()
                position = state["position"]
            if (
                child.state != "succeeded"
                and not expected_partial_failure
                and not cancelling
            ):
                # (A cancel never parks here: a child in a state it cannot read is
                # recorded as ended below and the cancel goes on, so it completes.)
                return self._failed_in_session(
                    session,
                    application,
                    state,
                    f"Run/Switch child returned {child.state}",
                )
            if not expected_partial_failure:
                children = list(sequence(state.get("children")) or ())
                # Record the child's public result tree with the child. A
                # profile step receipt is read back from this mirror, so a
                # completion that only updated progress would lose the child
                # receipt on this tick and again after restart.
                receipt = self._child_receipt(child)
                children.append(
                    {
                        "operation_id": child.operation_id,
                        "kind": active_kind,
                        # A cancelled load records a child it cannot read as ended
                        # by the cancel; the receipt vocabulary is closed.
                        "state": child.state
                        if child.state in {"succeeded", "failed", "cancelled"}
                        else "cancelled",
                        "result": (
                            receipt.model_dump(mode="json")
                            if receipt is not None
                            else None
                        ),
                    }
                )
                state["children"] = children
                state["active_operation_id"] = None
                state["active_kind"] = None
                state["position"] = (integer(position) or 0) + 1
                self._write_state(session, application, state)
                session.flush()
                position = state["position"]
            if cancelling:
                pending_cancellation = self._observe_superseded_agent_effects(
                    session, application, state
                )
                if pending_cancellation is not None:
                    return pending_cancellation
                return self._finish_cancelled_in_session(session, application, state)
        if cancelling:
            pending_cancellation = self._observe_superseded_agent_effects(
                session, application, state
            )
            if pending_cancellation is not None:
                return pending_cancellation
            return self._finish_cancelled_in_session(session, application, state)
        queue = sequence(state.get("queue"))
        if queue is None or (integer(position) or 0) >= len(queue):
            pending_cancellation = self._observe_superseded_agent_effects(
                session, application, state
            )
            if pending_cancellation is not None:
                return pending_cancellation
            reviewed = self._child_review(session, application_id)
            incomplete_model = (
                None
                if isinstance(reviewed, Residue)
                else next(
                    (
                        reason
                        for reason in reviewed.reasons
                        if reason.code == ProfileReasonCode.INCOMPLETE_MULTI_SPARK_MODEL
                    ),
                    None,
                )
            )
            failures = sequence(state.get("assignment_failures")) or ()
            # The load's state is the aggregate of its children: a recorded failure
            # (or a model the reviewed plan could not complete) is a failed child;
            # otherwise every child ended well.
            doc_state(
                state,
                "failed"
                if incomplete_model is not None or failures
                else _stored_state(
                    FleetProfileAdapter.recorded_aggregate(
                        sequence(state.get("children")) or ()
                    )
                ),
            )
            first_failure = next(
                (
                    str(failure.get("reason"))
                    for failure in failures
                    if isinstance(failure, Mapping)
                ),
                None,
            )
            state["status_reason"] = (
                incomplete_model.detail
                if incomplete_model is not None
                else (
                    f"{len(failures)} assignment(s) need reconciliation: "
                    f"{first_failure}"[:512]
                    if failures
                    else None
                )
            )
            state["result"] = {
                "children": list(sequence(state.get("children")) or ()),
                "assignment_ids": list(sequence(state.get("assignment_ids")) or ()),
            }
            self._write_state(session, application, state)
            session.flush()
            return self._view_from_state(application, state)
        item = queue[integer(position) or 0]
        if isinstance(item, Mapping) and item.get("kind") == "stop":
            stopped = session.get(RecipeRun, item.get("id"))
            if stopped is None or stopped.state not in STOPPABLE_RUN_STATES:
                # The Run/Switch that replaced this workload already stopped
                # it, right before its successor started.
                _LOGGER.info(
                    "profile application %s: workload %s is already stopped by "
                    "its replacement",
                    application_id,
                    item.get("id"),
                )
                state["position"] = (integer(position) or 0) + 1
                self._write_state(session, application, state)
                session.flush()
                return self._view_from_state(application, state)
        if not isinstance(item, Mapping):
            failures = list(sequence(state.get("assignment_failures")) or ())
            failures.append(
                {
                    "assignment_id": None,
                    "reason": "Persisted assignment queue item is malformed",
                    "terminal": False,
                }
            )
            state["assignment_failures"] = failures
            state["position"] = (integer(position) or 0) + 1
            self._write_state(session, application, state)
            session.flush()
            return self._view_from_state(application, state)
        operation = self._start_child(
            application_id,
            item,
            assignments,
            tuple(
                str(node_id)
                for node_id in (sequence(state.get("scope_node_ids")) or ())
            ),
            str(state["actor"]),
            str(state["request_id"]),
            integer(position) or 0,
            _persisted_profile_progress(application).workload_intent_ordinal,
        )
        if isinstance(operation, Residue):
            # This assignment's step cannot be issued (its identity or its bound
            # child cannot be established).  It is recorded as that assignment's
            # failure and the queue goes on with the next one: one damaged element
            # never stops the others, and the load's automatic recovery retries
            # the recorded failure with a fresh child identity.
            failures = list(sequence(state.get("assignment_failures")) or ())
            failures.append(
                {
                    "assignment_id": (
                        item.get("assignment_id") if isinstance(item, Mapping) else None
                    ),
                    "reason": f"{operation.reason.value}: {operation.note}"[:512],
                    "terminal": False,
                }
            )
            state["assignment_failures"] = failures
            state["position"] = (integer(position) or 0) + 1
            self._write_state(session, application, state)
            session.flush()
            return self._view_from_state(application, state)
        state["active_operation_id"] = operation.operation_id
        state["active_kind"] = item.get("kind")
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_child(application_id, state, operation)

    def _observed_child(self, operation_id: str) -> RunSwitchOperation | None:
        """The Run/Switch child, or ``None`` when its record is gone (recorded)."""

        try:
            return self._run_switch.get(operation_id)
        except KeyError:
            retire_as_unknown(
                "profile-child",
                operation_id,
                BookkeepingReason.ROW_INCOMPLETE,
                "the Run/Switch child is not stored",
            )
            return None

    def _observe_superseded_agent_effects(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: dict[str, object],
    ) -> FleetProfileChildOperation | None:
        """Wait for older issued cancellation receipts before a switch succeeds."""

        now = _aware(self._run_switch._clock())
        raw_due = state.get("observation_due_at")
        if (
            state.get("state") == "running"
            and isinstance(raw_due, str)
            and now < _aware(datetime.fromisoformat(raw_due))
        ):
            return self._view_from_state(application, state)
        progress = _persisted_profile_progress(application)
        ordinal = progress.workload_intent_ordinal
        cancellation = progress.cancellation
        if cancellation is not None:
            ordinal = cancellation.workload_intent_ordinal
        if ordinal is None:
            # A load that never recorded a workload intent fenced no older
            # effects, so there is none to wait for: its own child is what it
            # stops or completes (a cancel or an adopted older run alike).
            return None
        scope_node_ids = _string_items(
            state.get("scope_node_ids"),
            fallback=_persisted_profile_scope(application) or (),
        )
        AgentJobService.abandon_superseded_idempotent_operations_in_session(
            session, scope_node_ids, ordinal, now
        )
        effects = AgentJobService.assess_superseded_agent_effects_in_session(
            session, scope_node_ids, ordinal, now
        )
        if not effects:
            state["observation_due_at"] = None
            state["observation_deadline_at"] = None
            state["pending_operation_ids"] = []
            state["status_reason"] = None
            state["stop_reissue_attempt"] = 0
            return None
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, scope_node_ids, ordinal, now
        )
        deadline = min(effect.observation_deadline for effect in effects)
        due = min(effect.observe_due_at for effect in effects)
        # Each re-issue backs off exponentially (persisted attempt counter), so
        # an effect past its observation deadline is not re-stopped every tick.
        raw_attempt = state.get("stop_reissue_attempt")
        attempt = (raw_attempt if type(raw_attempt) is int else 0) + 1
        due = max(due, FleetProfileAdapter.next_retry(application.id, attempt, now))
        operation_ids = sorted(effect.operation_id for effect in effects)
        reason = (
            "Reissuing Stop and observing older issued workload cancellation: "
            + ", ".join(operation_ids)
        )
        updated = {
            "status_reason": reason[:512],
            "observation_due_at": due.isoformat(),
            "observation_deadline_at": deadline.isoformat(),
            "pending_operation_ids": operation_ids,
            "stop_reissue_attempt": min(attempt, 32),
        }
        if state.get("state") != "running" or any(
            state.get(key) != value for key, value in updated.items()
        ):
            doc_state(state, "running")
            state.update(updated)
            self._write_state(session, application, state)
            session.flush()
        return self._view_from_state(application, state)

    def _finish_cancelled_in_session(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: dict[str, object],
    ) -> FleetProfileChildOperation:
        doc_state(state, "cancelled")
        state["status_reason"] = "Profile effects were reconciled after cancellation"
        state["result"] = {
            "children": list(sequence(state.get("children")) or ()),
            "assignment_ids": list(sequence(state.get("assignment_ids")) or ()),
        }
        self._write_state(session, application, state)
        cancellation = _persisted_profile_progress(application).cancellation
        if cancellation is not None:
            progress = _persisted_profile_progress(application)
            progress_data = progress.model_dump(mode="json")
            cancellation_state(progress_data, "cancelled")
            application.progress = read_stored_model(
                FleetProfileApplicationProgress,
                canonical_message(progress_data),
                strict=True,
                from_json=True,
            ).model_dump(mode="json")
        session.flush()
        return self._view_from_state(application, state)

    def _start_child(
        self,
        application_id: str,
        item: Mapping[str, object],
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
        position: int,
        workload_intent_ordinal: int | None,
    ) -> RunSwitchOperation | Residue:
        """Issue (or adopt) the child of one queue item, or say why it cannot be.

        A :class:`Residue` is that element's unknown: the caller records it as
        that assignment's failure and continues with the next element.
        """

        child_request_key = profile_switch_child_request_key(
            application_id, position, str(item.get("kind")), str(item.get("id"))
        )
        kind = item.get("kind")
        adopted = self._adopt_child(
            child_request_key,
            application_id=application_id,
            item=item,
            assignments=assignments,
            scope_node_ids=scope_node_ids,
            workload_intent_ordinal=workload_intent_ordinal,
        )
        if adopted is not None:
            return adopted
        if workload_intent_ordinal is None:
            # An effect that already exists is adopted (above); a new one is fenced
            # by its intent, and without one nothing new may be issued (the
            # admission that records it runs again).
            return retire_as_unknown(
                "profile-child",
                application_id,
                BookkeepingReason.ROW_INCOMPLETE,
                "the application recorded no workload intent",
            )
        with self._sessions() as session:
            reviewed = self._child_review(session, application_id)
        if isinstance(reviewed, Residue):
            return reviewed
        if kind == "cleanup":
            installation_id = item.get("id")
            if not isinstance(installation_id, str):
                return retire_as_unknown(
                    "profile-child",
                    application_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "a cleanup item has no installation identity",
                )
            cleanup_preview = self._run_switch.preview_cleanup(
                RunSwitchCleanupPreviewRequest(installation_id=installation_id),
                actor=actor,
            )
            self._validate_child_effects(reviewed, cleanup_preview)
            return self._run_switch.apply_cleanup(
                RunSwitchCleanupApplyRequest(
                    installation_id=installation_id,
                    request_key=child_request_key,
                ),
                actor=actor,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        if kind == "stop":
            run_id = item.get("id")
            if not isinstance(run_id, str):
                return retire_as_unknown(
                    "profile-child",
                    application_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "a stop item has no run identity",
                )
            raw_scope = item.get("profile_stop_scope")
            profile_stop_scope = (
                read_stored_model(
                    RunSwitchProfileStopScope,
                    canonical_message(raw_scope),
                    strict=True,
                    from_json=True,
                )
                if isinstance(raw_scope, Mapping)
                else None
            )
            preview = (
                self._run_switch.preview_stop(
                    RunSwitchStopPreviewRequest(run_id=run_id), actor=actor
                )
                if profile_stop_scope is None
                else self._run_switch.preview_profile_stop(
                    run_id, profile_stop_scope, actor=actor
                )
            )
            self._validate_child_effects(reviewed, preview)
            if profile_stop_scope is not None:
                return self._run_switch.apply_profile_stop(
                    run_id,
                    profile_stop_scope,
                    plan_digest=preview.plan_digest,
                    request_key=child_request_key,
                    actor=actor,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_application_id=application_id,
                )
            return self._run_switch.apply_stop(
                RunSwitchStopApplyRequest(
                    run_id=run_id,
                    plan_digest=preview.plan_digest,
                    request_key=child_request_key,
                ),
                actor=actor,
                workload_intent_ordinal=workload_intent_ordinal,
                profile_application_id=application_id,
            )
        assignment_id = item.get("id")
        assignment = next(
            (value for value in assignments if value.id == assignment_id), None
        )
        if assignment is None:
            return retire_as_unknown(
                "profile-child",
                application_id,
                BookkeepingReason.ROW_INCOMPLETE,
                f"assignment {assignment_id} is not part of the accepted intent",
            )
        with self._sessions() as session:
            request = self._assignment_request(
                session, assignment, request_key=child_request_key
            )
            if isinstance(request, Residue):
                return request
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            accepted = FleetProfileService._reviewed_profile_plan(
                application, session=session
            )
            if isinstance(accepted, Residue):
                return accepted
            progress = _persisted_profile_progress(application)
            expected_preparation = next(
                (
                    item.preparation
                    for item in accepted.preparations
                    if item.assignment_id == assignment.id
                ),
                None,
            )
        plan = self._run_switch.preview(
            request, actor=actor, profile_application_id=application_id
        )
        if expected_preparation is not None or progress.retry_of_application_id:
            _require_recovery_preparation(
                assignment.id,
                expected_preparation,
                plan.preparation,
                first_binding=not accepted.allowed,
            )
        if not plan.allowed:
            raise FleetProfileChildPlanBlocked(
                "profile child plan blocked: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        self._validate_child_effects(reviewed, plan)
        return self._run_switch.apply(
            request.model_copy(update={"plan_digest": plan.plan_digest}),
            actor=actor,
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=application_id,
        )

    @staticmethod
    def _child_review(
        session: Session, application_id: str
    ) -> FleetProfilePreview | Residue:
        application = session.get(FleetProfileApplication, application_id)
        if application is None:
            raise MissingRecord(application_id, reason=InvalidRequestReason.NOT_FOUND)
        intended = FleetProfileService._intended_profile(application, session=session)
        if isinstance(intended, Residue):
            return intended
        return _persisted_profile_plan(application)

    @staticmethod
    def _validate_child_effects(
        reviewed: FleetProfilePreview, child: RunSwitchPlan
    ) -> None:
        """A fresh child plan cannot enlarge the accepted parent's consent."""
        execution_nodes = {node for step in reviewed.steps for node in step.node_ids}
        child_targets = (
            set(child.profile_stop_scope.target_node_ids)
            if child.profile_stop_scope is not None
            else {node.node_id for node in child.spark_group.nodes}
        )
        if not child_targets <= execution_nodes:
            raise FleetProfileReviewStale(
                "Profile child exceeds its reviewed Spark scope"
            )
        stops = {
            effect.run_id: effect
            for effect in reviewed.effects.runs
            if effect.action == "stop"
        }
        for stop in child.stops:
            expected = stops.get(stop.run_id)
            if expected is None or expected.alias != stop.alias:
                raise FleetProfileReviewStale(
                    "Profile child would stop an unreviewed workload; review again"
                )
            if expected.profile_stop_scope is None:
                valid_stop_scope = child.profile_stop_scope is None and sorted(
                    expected.node_ids
                ) == sorted(stop.node_ids)
            else:
                valid_stop_scope = (
                    child.profile_stop_scope == expected.profile_stop_scope
                    and stop.node_ids == expected.profile_stop_scope.target_node_ids
                )
            if not valid_stop_scope:
                raise FleetProfileReviewStale(
                    "Profile child changed its reviewed Stop target scope"
                )
        if child.action == "cleanup" and not any(
            effect.action == "remove"
            and effect.installation_id == child.installation_id
            and sorted(effect.node_ids)
            == sorted(node.node_id for node in child.spark_group.nodes)
            for effect in reviewed.effects.installations
        ):
            raise FleetProfileReviewStale(
                "Profile child would remove an unreviewed installation; review again"
            )

    def _adopt_child(
        self,
        request_key: str,
        *,
        application_id: str,
        item: Mapping[str, object],
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        workload_intent_ordinal: int | None,
    ) -> RunSwitchOperation | Residue | None:
        """Read the exact committed child before any mutable preview is redone.

        A committed child that cannot be shown to be *this* step's child (another
        kind, intent, scope, capacity owner or assignment, or a plan that cannot be
        read) is not adopted and not reissued: it stays with its own Run/Switch
        lifecycle, and the step is recorded as unknown for the caller to carry on.
        """

        def unknown(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown("profile-child", request_key, reason, note)

        with self._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_key))
            if job is None:
                return None
            kind = item.get("kind")
            if not isinstance(kind, str):
                return unknown(
                    BookkeepingReason.ROW_INCOMPLETE, "the queue item names no kind"
                )
            expected_kind = {
                "cleanup": "recipe.cleanup.v2",
                "stop": "recipe.stop.v2",
                "run": "recipe.run-switch.v2",
                "install": "recipe.run-switch.v2",
            }.get(kind)
            if job.kind != expected_kind:
                raise FleetProfileInvalid(
                    "Profile child request key belongs to another operation",
                    reason=InvalidRequestReason.CONFLICT,
                )
            recorded_ordinal = job.payload.get("workload_intent_ordinal")
            if (
                recorded_ordinal is not None
                and workload_intent_ordinal is not None
                and recorded_ordinal != workload_intent_ordinal
            ):
                # (A child issued before intents were recorded carries none, and
                # an application that recorded none adopts what it issued: an old
                # run survives an upgrade.)
                return unknown(
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the child belongs to another workload intent",
                )
            raw_plan = job.payload.get("plan")
            plan = read_or_rebuild(
                kind="profile-child-plan",
                subject=str(job.id),
                read=lambda: read_stored_model(
                    RunSwitchPlan,
                    canonical_message(raw_plan),
                    strict=True,
                    from_json=True,
                ),
            )
            if isinstance(plan, Residue):
                return plan
            child_nodes = tuple(
                sorted(
                    plan.profile_stop_scope.target_node_ids
                    if plan.profile_stop_scope is not None
                    else [node.node_id for node in plan.spark_group.nodes]
                )
            )
            if child_nodes != tuple(sorted(job.targets)) or not set(child_nodes) <= set(
                scope_node_ids
            ):
                return unknown(
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the child's Spark scope differs from the bound scope",
                )
            owner_id = item.get("id")
            if kind == "cleanup":
                valid = plan.action == "cleanup" and plan.installation_id == owner_id
            elif kind == "stop":
                raw_scope = item.get("profile_stop_scope")
                expected_scope = (
                    read_stored_model(
                        RunSwitchProfileStopScope,
                        canonical_message(raw_scope),
                        strict=True,
                        from_json=True,
                    )
                    if isinstance(raw_scope, Mapping)
                    else None
                )
                valid = (
                    plan.action == "stop"
                    and plan.run_id == owner_id
                    and plan.profile_stop_scope == expected_scope
                    and (
                        expected_scope is None
                        or child_nodes == tuple(expected_scope.target_node_ids)
                    )
                )
            else:
                receipt = read_or_rebuild(
                    kind="profile-child-receipt",
                    subject=str(job.id),
                    read=lambda: read_stored_model(
                        RunSwitchOperationResult,
                        canonical_message(job.result),
                        strict=True,
                        from_json=True,
                    ),
                )
                if isinstance(receipt, Residue):
                    return receipt
                if receipt.profile_application_id != application_id:
                    return unknown(
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "the child's capacity is owned by another application",
                    )
                assignment = next(
                    (value for value in assignments if value.id == owner_id), None
                )
                expected = (
                    self._assignment_intent(assignment)
                    if assignment is not None
                    else None
                )
                valid = (
                    assignment is not None
                    and expected is not None
                    and kind
                    == (
                        "install"
                        if assignment.desired_state == DesiredAssignmentState.INSTALLED
                        else "run"
                    )
                    and (plan.action, plan.alias) == expected
                    and plan.recipe_revision_id == assignment.recipe_revision_id
                    and [node.model_dump() for node in plan.spark_group.nodes]
                    == [
                        node.model_dump()
                        for node in sorted(assignment.nodes, key=lambda node: node.rank)
                    ]
                )
            if not valid:
                return unknown(
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the child differs from its bound owner or assignment",
                )
            reviewed = self._child_review(session, application_id)
            if isinstance(reviewed, Residue):
                return reviewed
            self._validate_child_effects(reviewed, plan)
            operation_id = job.id
        return self._run_switch.get(operation_id)

    def _plan_queue(
        self,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        *,
        application_id: str,
        installation_policy: str,
        reviewed_effects: FleetProfileEffects,
        expected_images: Mapping[str, RuntimeImageIdentity],
    ) -> list[dict[str, object]]:
        control = FleetProfileService._control_effects(
            session,
            assignments,
            set(scope_node_ids),
            installation_policy,
            expected_images=expected_images,
            excluded_application_id=application_id,
        )
        if any(reason.severity == "error" for reason in control.reasons):
            raise FleetProfileInvalid(
                "Profile workload effects can no longer be represented safely; review again",
                reason=InvalidRequestReason.SUPERSEDED,
            )
        _validate_remaining_effects(reviewed_effects, control.effects)
        return _switch_queue(
            [effect for effect in control.effects.runs if effect.action == "stop"],
            [
                assignment
                for assignment in assignments
                if assignment.id not in control.unavailable_assignment_ids
                if control.states[assignment.id].current_state
                != assignment.desired_state
            ],
            [
                effect.installation_id
                for effect in control.effects.installations
                if effect.action == "remove"
            ],
        )

    @staticmethod
    def _state(application: FleetProfileApplication) -> dict[str, object] | None:
        # Persisted JSON is consumed with the canonical JSON validation
        # semantics, exactly as it was written.  Validating an already-decoded
        # document as strict Python data rejects JSON arrays where the contract
        # declares tuples, which makes Pydantic's smart union pick a different
        # phase-result variant and loses the receipt entirely.  Damaged progress
        # is rebuilt from the row's receipt (no child document), so the child is
        # found again through its deterministic request key, never refused.
        raw = _persisted_profile_progress(application).switch_adapter
        if raw is None:
            return None
        return raw.model_dump(mode="json")

    def _save_state(self, application_id: str, state: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            application = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(application)
            if progress.cancellation is not None and progress.switch_adapter is None:
                raise FleetProfileInvalid(
                    "Profile cancellation prevents child state creation",
                    reason=InvalidRequestReason.CONFLICT,
                )
            self._write_state(session, application, state)

    @staticmethod
    def _write_state(
        session: Session,
        application: FleetProfileApplication,
        state: Mapping[str, object],
    ) -> None:
        typed = read_stored_model(
            FleetProfileSwitchAdapterState,
            canonical_message(state),
            strict=True,
            from_json=True,
        )
        progress = read_stored_model(
            FleetProfileApplicationProgress,
            canonical_message(application.progress),
            strict=True,
            from_json=True,
        )
        application.progress = {
            **progress.model_dump(mode="json"),
            "switch_adapter": typed.model_dump(mode="json"),
        }

    def _failed_in_session(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: dict[str, object],
        reason: str,
    ) -> FleetProfileChildOperation:
        doc_state(state, "failed")
        state["status_reason"] = reason[:512]
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_state(application, state)

    @staticmethod
    def _assignments_from_state(
        state: Mapping[str, object], application: FleetProfileApplication
    ) -> tuple[FleetProfileAssignment, ...]:
        """The assignments a child was started with, rebuilt from the accepted intent.

        An element of the stored snapshot that cannot be read is skipped.  When
        the snapshot is missing or unreadable as a whole, the application's own
        accepted intent is the evidence it was copied from, narrowed to the
        assignments the child names.
        """

        raw = state.get("assignments")
        values: list[FleetProfileAssignment] = []
        damaged = not isinstance(raw, list)
        for item in raw if isinstance(raw, list) else ():
            try:
                values.append(read_stored_model(FleetProfileAssignment, item))
            except ValueError:
                damaged = True
        if damaged:
            wanted = {
                value
                for value in (sequence(state.get("assignment_ids")) or ())
                if isinstance(value, str)
            }
            intent = _persisted_profile_progress(application).intended_profile
            known = {value.id for value in values}
            rebuilt = [
                assignment
                for assignment in (intent.assignments if intent is not None else ())
                if assignment.id in wanted and assignment.id not in known
            ]
            if rebuilt:
                _LOGGER.info(
                    "profile application %s: rebuilt %d assignment(s) of its child "
                    "from the accepted intent",
                    application.id,
                    len(rebuilt),
                )
            values.extend(rebuilt)
        return tuple(sorted(values, key=lambda item: item.id))

    @staticmethod
    def _child_receipt(
        child: RunSwitchOperation,
    ) -> FleetProfileSwitchChildResult | None:
        """Return the child's public result tree as a profile step receipt."""

        if child.result is None:
            return None
        try:
            return FleetProfileSwitchChildResult(
                run_switch_operation_id=child.operation_id,
                run_switch=read_stored_model(
                    RunSwitchOperationResult,
                    canonical_message(child.result),
                    strict=True,
                    from_json=True,
                ),
            )
        except (TypeError, ValueError) as error:
            # The child's own record keeps its result; the profile step is
            # recorded without a receipt rather than refusing to record it.
            retire_as_unknown(
                "profile-child-receipt",
                str(child.operation_id),
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                stored_document_detail(error) or type(error).__name__,
            )
            return None

    def _view_from_child(
        self,
        application_id: str,
        state: Mapping[str, object],
        child: RunSwitchOperation,
    ) -> FleetProfileChildOperation:
        run_phase = child.current_phase or child.progress.phase or "prepare"
        phase = _PROFILE_PHASE_ADAPTER.validate_python(
            _PROFILE_PHASE_BY_RUN_PHASE.get(run_phase, run_phase), strict=True
        )
        progress = FleetProfileChildProgress(
            operation=child.progress.operation,
            startup_budget_seconds=child.progress.startup_budget_seconds,
            start_deadline=child.progress.start_deadline,
            phase=phase,
            node_ids=_string_items(state.get("scope_node_ids")),
            bytes=child.progress.completed_bytes,
            total_bytes=child.progress.total_bytes,
        )
        child_state = _operation_state(
            "running"
            if child.state
            in job_states.words(LifecycleState.OBSERVING, LifecycleState.NEEDS_OPERATOR)
            else child.state,
            default=LifecycleState.RUNNING,
        )
        result = self._child_receipt(child)
        attempt = child.result.retry_attempt if child.result is not None else None
        return FleetProfileChildOperation(
            id=application_id,
            state=child_state,
            progress=progress,
            status_reason=child.status_reason,
            result=result,
            # A phase the child keeps failing is the stall an operator must see;
            # a first retry or a short contention hold is not.
            stalls=[
                blocker
                for blocker in child.blockers
                if blocker.code == PHASE_RETRY_CODE
                and (attempt or 0) >= STALL_RETRY_ATTEMPT
            ],
        )

    @staticmethod
    def _view_from_state(
        application: FleetProfileApplication,
        state: Mapping[str, object],
    ) -> FleetProfileChildOperation:
        child_state = _operation_state(
            state.get("state"), default=LifecycleState.RUNNING
        )
        raw_progress = state.get("child_progress")
        progress = (
            # The stored document is JSON that the database driver already
            # decoded, so it must be read with the contract's JSON semantics:
            # Python mode refuses the ISO string this same model serializes for
            # ``start_deadline``, which failed a whole live application after
            # its distribution and install had already succeeded.
            read_stored_model(
                FleetProfileChildProgress, json.dumps(raw_progress), from_json=True
            )
            if isinstance(raw_progress, Mapping)
            else FleetProfileChildProgress(
                phase="final-verify" if child_state == "succeeded" else "prepare",
                node_ids=_string_items(state.get("scope_node_ids")),
            )
        )
        raw_status_reason = state.get("status_reason")
        return FleetProfileChildOperation(
            id=application.id,
            state=child_state,
            progress=progress,
            status_reason=(
                raw_status_reason if isinstance(raw_status_reason, str) else None
            ),
            result=_state_receipt(state),
        )


def _state_receipt(state: Mapping[str, object]) -> FleetProfileChildResult | None:
    """Return the newest completed child receipt held by a mirrored state.

    A profile step's recorded result is read from the mirror, so the receipt of
    the child that finished last is the step's evidence.  States written before
    receipts were mirrored fall back to the adapter's own summary.
    """

    children = sequence(state.get("children")) or ()
    for raw in reversed(list(children)):
        if not isinstance(raw, Mapping):
            continue
        receipt = raw.get("result")
        if isinstance(receipt, Mapping):
            return read_stored_model(
                FleetProfileSwitchChildResult,
                canonical_message(receipt),
                strict=True,
                from_json=True,
            )
    summary = state.get("result")
    return (
        read_stored_model(
            FleetProfileSwitchAdapterResult,
            canonical_message(summary),
            strict=True,
            from_json=True,
        )
        if isinstance(summary, Mapping)
        else None
    )


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
                    kind="profile-step",
                    label=f"Completed profile step {key}",
                    operation_id=receipt.operation_id,
                    outcome="succeeded",
                )
            )
        for step in plan.steps[application.current_step :]:
            cancelled.append(
                FleetProfileApplicationEffect(
                    effect_id=f"step:{step.index}",
                    kind="profile-step",
                    label=f"Not issued profile step {step.index + 1}: {step.label}",
                    outcome="not-issued",
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
                    "cancelled"
                    if child.state == "cancelled"
                    else "failed"
                    if child.state == "failed"
                    else "succeeded"
                ),
            )
            (cancelled if child.state == "cancelled" else completed).append(effect)
        active_id = adapter.active_operation_id
        if active_id is not None:
            pending.append(
                FleetProfileApplicationEffect(
                    effect_id=active_id,
                    kind=adapter.active_kind or "run",
                    label="Active Run/Switch child",
                    operation_id=active_id,
                    outcome="pending",
                )
            )
        pending_ids = sorted(
            set(adapter.pending_operation_ids) | set(intent.pending_operation_ids)
        )
        for operation_id in pending_ids:
            if operation_id == active_id:
                continue
            pending.append(
                FleetProfileApplicationEffect(
                    effect_id=operation_id,
                    kind="agent-operation",
                    label="Issued agent effect awaiting its receipt",
                    operation_id=operation_id,
                    outcome="pending",
                )
            )
        queue_start = adapter.position + (1 if active_id else 0)
        for index, item in enumerate(adapter.queue[queue_start:], start=queue_start):
            cancelled.append(
                FleetProfileApplicationEffect(
                    effect_id=f"queue:{index}:{item.kind}:{item.id}",
                    kind=item.kind,
                    label=f"Not issued {item.kind} effect {item.id}",
                    outcome="not-issued",
                )
            )
        # A later whole-profile step can remain after the adapter's current
        # switch queue; expose that reviewed work as not issued too.
        for step in plan.steps[application.current_step + 1 :]:
            cancelled.append(
                FleetProfileApplicationEffect(
                    effect_id=f"step:{step.index}",
                    kind="profile-step",
                    label=f"Not issued profile step {step.index + 1}: {step.label}",
                    outcome="not-issued",
                )
            )

    pending.extend(
        FleetProfileApplicationEffect(
            effect_id=operation_id,
            kind="agent-operation",
            label="Issued agent effect awaiting its receipt",
            operation_id=operation_id,
            outcome="pending",
        )
        for operation_id in pending_ids
        if operation_id not in {effect.effect_id for effect in pending}
    )
    dependency = adapter.active_operation_id if adapter is not None else None
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
) -> list[dict[str, object]]:
    """Order a profile's switch steps so replaced workloads keep serving.

    A Run/Switch stops the workloads on its own Sparks right before it starts.
    A workload that one new run replaces on the same Sparks is therefore left
    serving until then: its stop step comes after the new work and finds
    nothing left to do. A workload no single new run covers (for example one
    spread over Sparks two new runs take separately) is stopped first.
    """

    run_groups = [
        {node.node_id for node in assignment.nodes}
        for assignment in work
        if assignment.desired_state == DesiredAssignmentState.RUNNING
    ]

    def stop_item(effect: FleetProfileRunEffect) -> dict[str, object]:
        return {
            "kind": "stop",
            "id": effect.run_id,
            **(
                {
                    "profile_stop_scope": effect.profile_stop_scope.model_dump(
                        mode="json"
                    )
                }
                if effect.profile_stop_scope is not None
                else {}
            ),
        }

    replaced = [
        effect
        for effect in stops
        if any(set(effect.node_ids) <= group for group in run_groups)
    ]
    return [
        *(stop_item(effect) for effect in stops if effect not in replaced),
        *(
            {
                "kind": "install"
                if assignment.desired_state == DesiredAssignmentState.INSTALLED
                else "run",
                "id": assignment.id,
            }
            for assignment in work
        ),
        *(stop_item(effect) for effect in replaced),
        *({"kind": "cleanup", "id": installation_id} for installation_id in removals),
    ]


def _validate_remaining_effects(
    reviewed: FleetProfileEffects, remaining: FleetProfileEffects
) -> None:
    """Recovery may finish destructive effects, never acquire new targets."""
    reviewed_stops = {
        _digest(effect) for effect in reviewed.runs if effect.action == "stop"
    }
    reviewed_removals = {
        _digest(effect)
        for effect in reviewed.installations
        if effect.action == "remove"
    }
    if any(
        effect.action == "stop" and _digest(effect) not in reviewed_stops
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
    document: Mapping[str, object],
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
        recipe = read_recipe(document)
    except ValueError:
        return dict(stored), []
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


# Execution IDs remain deterministic across previews, while logical authoring
# changes (choice, variant, group or desired state) produce a new assignment.
_assignment_id = _choice_id


def _expanded_roles(topology: RecipeTopology) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (role.name, role.endpoint_owner)
        for role in topology.roles
        for _ in range(role.count)
    )


def _profile_document(row: FleetProfile) -> dict[str, object]:
    return {
        "schema_version": 2,
        "id": row.id,
        "number": row.number,
        "revision": row.revision,
        "name": row.name,
        "description": row.description,
        "installation_policy": row.installation_policy,
        "labels": dict(row.labels),
        "favorite": row.favorite,
        "assignments": list(row.assignments),
    }


def build_production_fleet_profile_service(
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime],
    run_switch_operations: RunSwitchOperationService,
    cache_resolver: Callable[..., Mapping[str, object]] | None = None,
) -> FleetProfileService:
    """Compose the Controller's Fleet profile service and its authority.

    The Run/Switch adapter is both the apply boundary and the preparation
    provider.  Composing them in one place means a profile preview and the
    child operation it later queues always resolve the exact same model,
    recipe revision, target group and runtime image identity.  It also makes
    the preparation binding impossible to drop by accident: a deployment
    cannot construct this service without a provider that can attest the
    assets a plan requires.
    """

    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch_operations)
    return FleetProfileService(
        sessions,
        clock=clock,
        switch_adapter=adapter,
        cache_resolver=cache_resolver,
        assessment_provider=adapter.assess,
    )


class FleetProfileService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        switch_adapter: FleetProfileSwitchAdapter | None = None,
        cache_resolver: Callable[..., Mapping[str, object]] | None = None,
        assessment_provider: _AssessmentProvider | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._switch_adapter = switch_adapter
        self._cache_resolver = cache_resolver
        self._assessment_provider = assessment_provider
        self._preparation_starter: PreparationStarter | None = None
        self._storage_relief: StorageReliefProvider | None = None
        self._preparation_canceller: PreparationCanceller | None = None
        # The lifecycle adapter is the only writer of an application's state; its
        # hooks are this service's own claim release, child stop and observation.
        self._lifecycle = FleetProfileAdapter(
            sessions=sessions,
            clock=clock,
            stopper=self._stop_children,
            observer=self._observe_children,
            after_state=self._release_claims,
            finish=self._finish_cancel_records,
        )
        # Round-robin position of the bounded automatic-recovery scan, so rows
        # that stay ineligible cannot starve later due rows.
        self._recovery_cursor: str | None = None

    def _selected_profile_snapshot(
        self, session: Session
    ) -> _SelectedProfileSnapshot | Residue | None:
        """The selected profile's application and plan, ``None`` when nothing is
        selected, or a :class:`Residue` when the selection's receipt is damaged
        (recorded as unknown; the application worker fails that receipt)."""

        selection = session.get(FleetProfileSelection, 1)
        if selection is None:
            return None

        def read() -> _SelectedProfileSnapshot | Damaged:
            application = session.get(FleetProfileApplication, selection.application_id)
            if application is None or application.profile_id != selection.profile_id:
                return Damaged("selected profile application is unavailable")
            if application.selection_generation != selection.generation:
                return Damaged("selected profile generation is inconsistent")
            try:
                intended = self._intended_profile(application, session=session)
            except FleetProfileConflict as error:
                # The reviewed intent's integrity check refused it.
                return Damaged(str(error))
            plan = _persisted_profile_plan(application)
            if isinstance(intended, Residue) or isinstance(plan, Residue):
                return Damaged("selected profile evidence is unavailable")
            if (
                application.profile_digest != intended.profile_digest
                or tuple(intended.scope.node_ids) != tuple(plan.scope.node_ids)
                or selection.roster_digest != _roster_digest(intended.scope.node_ids)
                or plan.profile_revision != selection.profile_revision
            ):
                return Damaged("selected profile snapshot is inconsistent")
            return _SelectedProfileSnapshot(
                generation=selection.generation,
                profile_id=selection.profile_id,
                profile_revision=selection.profile_revision,
                application_id=selection.application_id,
                roster_node_ids=tuple(intended.scope.node_ids),
                roster_digest=selection.roster_digest,
                actor=application.actor,
                intended=intended,
                plan=plan,
            )

        return read_or_rebuild(
            kind="profile-selection",
            subject=str(selection.application_id),
            read=read,
        )

    @staticmethod
    def _application_is_current_selection(
        session: Session,
        application: FleetProfileApplication,
        progress: FleetProfileApplicationProgress | None = None,
    ) -> bool:
        if progress is None:
            progress = _persisted_profile_progress(application)
        generation = application.selection_generation
        selection = session.get(FleetProfileSelection, 1)
        if selection is None:
            return False
        accepted_intent = progress.intended_profile
        if accepted_intent is None:
            return False
        seen: set[str] = set()
        current = application
        current_progress = progress
        while True:
            if current.id in seen:
                return False
            seen.add(current.id)
            current_intent = current_progress.intended_profile
            if current_intent is None or current_intent != accepted_intent:
                return False
            generation = current.selection_generation
            if generation is not None:
                return bool(
                    selection.generation == generation
                    and selection.application_id == current.id
                    and selection.profile_id == current.profile_id
                    and current.profile_id == application.profile_id
                )
            retry_parent_id = current_progress.retry_of_application_id
            if retry_parent_id is None:
                return False
            current = session.get(FleetProfileApplication, retry_parent_id)
            if current is None:
                return False
            try:
                current_progress = _persisted_profile_progress(current)
            except FleetProfileConflict:
                return False

    @staticmethod
    def _authorize(session: Session, actor: str, *, mutation: bool = True) -> None:
        if mutation:
            serialize_user_authority(session)
        user = session.scalar(select(User).where(User.subject == actor))
        if not FleetProfileService._user_has_profile_authority(user, mutation=mutation):
            raise FleetProfilePermissionDenied(
                "Current profile authority is unavailable"
            )

    @staticmethod
    def _user_has_profile_authority(user: User | None, *, mutation: bool) -> bool:
        """Check current profile authority without acquiring mutation fences.

        Read-only views use this to explain a blocked roster reconciliation;
        mutation callers still serialize authority changes before checking it.
        """
        if (
            user is None
            or user.disabled_at is not None
            or (
                mutation
                and user.role
                not in MUTATION_ROLES[("POST", "/api/profile/{number}/load")]
            )
        ):
            return False
        try:
            Actor(user.subject, user.role)
        except ValueError:
            return False
        return True

    def _release_claims(
        self, session: Session, application: FleetProfileApplication
    ) -> None:
        """After every change of an application's state: release only the claims no
        assignment owns, in the same transaction."""

        release_unassigned_profile_claims(
            session, application, now=_aware(self._clock())
        )

    @contextmanager
    def _admission_session(
        self,
        actor: str,
        *,
        node_ids: Sequence[str] = (),
    ) -> Iterator[Session]:
        """Open the short transaction used to accept a reviewed profile plan.

        Node advisory locks and the explicit row locks below serialize the
        effects this transaction owns. A whole-table PostgreSQL fence would
        make routine heartbeat and telemetry writes unrelated fleet blockers;
        exact plan, roster, catalog and capacity checks remain authoritative.
        """
        with self._sessions.begin() as session:
            self._authorize(session, actor)
            try:
                acquire_admission_keys(
                    session,
                    tuple(node_admission_key(node_id) for node_id in node_ids),
                    holder="profile-admission",
                )
            except AdmissionLockBusy as error:
                raise FleetProfileAdmissionBusy(
                    "Profile admission is busy"
                    + (
                        f" ({error.holder} is changing a selected Spark)"
                        if error.holder
                        else ""
                    )
                    + "; review again after the current fleet, catalog, workload or capacity change completes",
                    holder=error.holder,
                ) from error
            except OperationalError as error:
                if is_admission_contention(error):
                    raise FleetProfileAdmissionBusy(
                        "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                    ) from None
                raise
            try:
                yield session
            except IntegrityError as error:
                diagnostic = getattr(error.orig, "diag", None)
                constraint = getattr(diagnostic, "constraint_name", None)
                sqlstate = getattr(error.orig, "sqlstate", None)
                # Never include SQL, parameters or raw driver error text.
                name = (
                    constraint
                    if isinstance(constraint, str)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,128}", constraint)
                    else "unknown constraint"
                )
                code = (
                    sqlstate
                    if isinstance(sqlstate, str)
                    and re.fullmatch(r"[0-9A-Z]{5}", sqlstate)
                    else "unknown"
                )
                raise FleetProfileAdmissionStorageError(
                    f"Profile admission database constraint {name} rejected the write (SQLSTATE {code}). "
                    "The database contract must be reconciled; this request is retained and will retry automatically."
                ) from None
            except AdmissionLockBusy as error:
                raise FleetProfileAdmissionEffectBusy(
                    "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                ) from error
            except OperationalError as error:
                if is_admission_contention(error):
                    raise FleetProfileAdmissionEffectBusy(
                        "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                    ) from None
                raise

    @staticmethod
    def _next_profile_number(session: Session) -> int:
        """Allocate the next stable user profile number without renumbering."""

        maximum = session.scalar(select(func.max(FleetProfile.number)))
        return max(1, int(maximum or 0) + 1)

    @staticmethod
    def _recipe_identity(session: Session, selector: str) -> tuple[str, str] | Residue:
        """Resolve the exact current identities without loading artifact documents.

        A recipe that has no active revision (every revision retired or removed)
        is a :class:`Residue`: a saved profile that names it shows the choice as
        needing attention and the catalog sync replaces the recipe in place.
        """

        normalized_selector = selector.strip().casefold()
        if normalized_selector.count("/") != 1:
            raise FleetProfileInvalid(
                "recipe selector must use canonical publisher/slug form",
                reason=InvalidRequestReason.MALFORMED,
            )
        publisher, slug = normalized_selector.split("/", 1)
        candidates = tuple(
            session.scalars(
                select(CatalogDocument.id)
                .where(
                    CatalogDocument.kind == "recipe",
                    CatalogDocument.publisher == publisher,
                    CatalogDocument.slug == slug,
                )
                .order_by(
                    CatalogDocument.publisher, CatalogDocument.slug, CatalogDocument.id
                )
            )
        )
        if len(candidates) != 1:
            raise FleetProfileInvalid(
                "recipe selector is not an exact unique active recipe",
                reason=InvalidRequestReason.CONFLICT,
            )
        document = candidates[0]
        revisions = tuple(
            session.scalars(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.document_id == document,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
                .order_by(
                    CatalogDocumentRevision.revision_number.desc(),
                    CatalogDocumentRevision.created_at.desc(),
                    CatalogDocumentRevision.id.desc(),
                )
                .limit(16)
            )
        )
        if not revisions:
            return retire_as_unknown(
                "recipe",
                selector,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the recipe has no active catalog revision",
            )
        # The newest revision this Controller can read; when none is readable
        # the newest one is kept so the choice can report what needs attention.
        for revision in revisions:
            try:
                read_catalog_document(revision)
            except ValueError:
                continue
            return document, revision.id
        return document, revisions[0].id

    @classmethod
    def _recipe_document(
        cls, session: Session, selector: str
    ) -> tuple[CatalogDocument, CatalogDocumentRevision] | Residue:
        for _ in range(2):
            identity = cls._recipe_identity(session, selector)
            if isinstance(identity, Residue):
                return identity
            document_id, revision_id = identity
            document = session.get(CatalogDocument, document_id)
            revision = session.get(CatalogDocumentRevision, revision_id)
            if document is not None and revision is not None:
                return document, revision
            # The catalog changed between the two reads: resolve it again.
        return retire_as_unknown(
            "recipe",
            selector,
            BookkeepingReason.EVIDENCE_MISMATCH,
            "the recipe catalog changed during resolution",
        )

    @staticmethod
    def _recipe_selector(document: CatalogDocument) -> str:
        return f"{document.publisher}/{document.slug}"

    def _resolve_choice(
        self, session: Session, choice: FleetProfileAssignmentInput
    ) -> (
        tuple[CatalogDocument, CatalogDocumentRevision, Mapping[str, object] | None]
        | Residue
    ):
        """Resolve one choice once for both presentation and execution.

        The cache is evidence about what is already prepared, never a gate: when
        the resolver fails, answers with an invalid contract, or answers for a
        revision other than the one selected, the choice resolves without cache
        evidence (shown as unknown) and is prepared when it is loaded.
        """

        resolved = self._recipe_document(session, choice.recipe_selector)
        if isinstance(resolved, Residue):
            return resolved
        document, revision = resolved
        cache: Mapping[str, object] | None = None
        if self._cache_resolver is not None:
            try:
                candidate = self._cache_resolver(
                    recipe_identity=document.id,
                    model_variant=choice.model_variant,
                    exact_revision_id=revision.id,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                retire_as_unknown(
                    "profile-cache-resolution",
                    document.id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    f"{type(error).__name__}: {error}",
                )
                return document, revision, None
            if not isinstance(candidate, Mapping):
                retire_as_unknown(
                    "profile-cache-resolution",
                    document.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the cache resolver returned an invalid contract",
                )
                return document, revision, None
            cache = candidate
            recipe_part = candidate.get("recipe")
            chosen_id = (
                recipe_part.get("recipe_revision_id")
                if isinstance(recipe_part, Mapping)
                else None
            )
            # The resolver is asked for the current head revision, so a
            # different revision back is a resolution defect, not an older
            # cached substitute.  Its evidence is not used (the profile is never
            # bound to bytes the operator did not select) and the choice
            # resolves without cache evidence.
            if isinstance(chosen_id, str) and chosen_id != revision.id:
                retire_as_unknown(
                    "profile-cache-resolution",
                    document.id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the cache resolver answered for another recipe revision",
                )
                return document, revision, None
        return document, revision, cache

    @staticmethod
    def _assignment_selector(choice: FleetProfileAssignmentInput) -> str:
        if choice.assignment_name is not None:
            return choice.assignment_name
        value = choice.recipe_selector.lower().replace("/", "-").replace(" ", "-")
        value = "".join(
            character if character.isalnum() or character in "-_." else "-"
            for character in value
        )
        value = value.strip("-_.") or "assignment"
        return value[:63].rstrip("-_.") or "assignment"

    @staticmethod
    def _unreadable_choice_count(row: FleetProfile) -> int:
        """How many saved choices of a profile cannot be read (and are left out)."""

        raw = row.assignments
        readable = len(FleetProfileService._choices(row))
        return max(0, len(raw) - readable) if isinstance(raw, list) else 1

    @staticmethod
    def _choices(row: FleetProfile) -> tuple[FleetProfileAssignmentInput, ...]:
        """The saved choices of a profile; an unreadable choice is retired.

        The profile is read as a whole first.  When that fails the choices are read
        one by one: a damaged choice is skipped (and logged), so one broken
        element never makes the rest of the draft unreadable.
        """

        def read_all() -> tuple[FleetProfileAssignmentInput, ...]:
            return tuple(
                _STORED_ASSIGNMENTS.validate_json(canonical_message(row.assignments))
            )

        def read_each() -> tuple[FleetProfileAssignmentInput, ...] | None:
            raw = row.assignments
            if not isinstance(raw, list):
                return None
            readable: list[FleetProfileAssignmentInput] = []
            for index, item in enumerate(raw):
                try:
                    readable.extend(
                        _STORED_ASSIGNMENTS.validate_json(canonical_message([item]))
                    )
                except (TypeError, ValueError):
                    retire_as_unknown(
                        "profile-choice",
                        f"{row.id}#{index}",
                        BookkeepingReason.PERSISTED_STATE_DAMAGED,
                        "a saved choice does not parse",
                    )
            return tuple(readable)

        value = read_or_rebuild(
            kind="profile-choices",
            subject=str(row.id),
            read=read_all,
            rebuild=read_each,
        )
        return () if isinstance(value, Residue) else value

    def _validate_draft_review_identity(
        self,
        session: Session,
        profile: FleetProfile,
        *,
        profile_digest: str,
        assignments: tuple[FleetProfileAssignment, ...],
    ) -> None:
        """Recheck the saved draft and exact recipe heads bound by a review."""

        if _digest(_profile_document(profile)) != profile_digest:
            raise FleetProfileStalePlanConflict(
                "Fleet profile changed during application admission; review again"
            )
        assignment_by_id = {item.id: item for item in assignments}
        choices = self._choices(profile)
        if set(assignment_by_id) != {_choice_id(choice) for choice in choices}:
            raise FleetProfileStalePlanConflict(
                "Profile assignment set changed during admission; review again"
            )
        for choice in choices:
            identity = self._recipe_identity(session, choice.recipe_selector)
            assignment = assignment_by_id[_choice_id(choice)]
            if isinstance(identity, Residue) or (
                assignment.recipe_id,
                assignment.recipe_revision_id,
            ) != tuple(identity):
                raise FleetProfileStalePlanConflict(
                    "Profile recipe head changed during admission; review again"
                )

    def _execution_assignments(
        self, session: Session, row: FleetProfile
    ) -> tuple[FleetProfileAssignment, ...]:
        """Resolve logical choices into strict, load-bound assignments.

        A choice may be an incomplete distributed draft.  The generated rank
        mapping remains deterministic so preview can report the topology
        blocker; it is never submitted unless the recipe's exact topology
        accepts the complete group.
        """

        result: list[FleetProfileAssignment] = []
        for choice in self._choices(row):
            resolved = self._resolve_choice(session, choice)
            if isinstance(resolved, Residue):
                # The recipe resolves to no active revision: it is not part of the
                # resolved set, and the preview names it as a reason that waits for
                # the catalog sync (it never loads a profile without it silently).
                continue
            document, revision, _cache = resolved
            topology = recipe_topology(revision.document)
            option_choices, _notes = _effective_option_choices(
                revision.document, choice.option_choices
            )
            roles = _expanded_roles(topology)
            nodes = [
                FleetProfileNode(
                    node_id=node_id,
                    rank=rank,
                    role=roles[rank][0] if rank < len(roles) else "unresolved",
                    endpoint_owner=(roles[rank][1] if rank < len(roles) else rank == 0),
                )
                for rank, node_id in enumerate(choice.spark_ids)
            ]
            alias = (
                self._assignment_selector(choice)
                if choice.desired_state == DesiredAssignmentState.RUNNING
                else None
            )
            result.append(
                FleetProfileAssignment(
                    id=_choice_id(choice),
                    recipe_revision_id=revision.id,
                    topology_name=topology.name,
                    desired_state=choice.desired_state,
                    alias=alias,
                    nodes=nodes,
                    option_choices=option_choices,
                    recipe_id=document.id,
                    recipe_title=document.title,
                    model_title=self._model_title(session, revision.document),
                )
            )
        return tuple(result)

    def list(self) -> FleetProfileList:
        now = _aware(self._clock())
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfile).order_by(FleetProfile.number.asc()).limit(128)
                )
            )
            return FleetProfileList(
                generated_at=now, profiles=[self._view(session, row) for row in rows]
            )

    def get(self, profile_id: str) -> FleetProfileView:
        with self._sessions() as session:
            row = session.get(FleetProfile, profile_id)
            if row is None:
                raise MissingRecord(profile_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._view(session, row)

    def get_number(self, number: int) -> FleetProfileView:
        if type(number) is not int or number < 1:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
            if row is None:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            return self._view(session, row)

    def read_number(self, number: int) -> FleetProfileView:
        """Read a stable unused number without creating persistent state."""

        try:
            return self.get_number(number)
        except KeyError:
            if type(number) is not int or number < 1:
                raise
            now = _aware(self._clock())
            with self._sessions() as session:
                roster = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.revoked_at.is_(None))
                        .order_by(AgentNode.node_id)
                    )
                )
                display_names = {
                    item.node_id: item.display_name
                    for item in session.scalars(
                        select(AgentNodeProfile).where(
                            AgentNodeProfile.node_id.in_(
                                [node.node_id for node in roster]
                            )
                        )
                    )
                }
            fleet: list[dict[str, object]] = [
                {
                    "selector": node.node_id,
                    "display_name": display_names.get(node.node_id, node.node_id),
                    "state": "Idle",
                }
                for node in roster
            ]
            document = {
                "schema_version": 2,
                "number": number,
                "revision": 0,
                "assignments": [],
            }
            return FleetProfileView(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:profile:{number}")),
                number=number,
                revision=0,
                name="Default" if number == 1 else f"Profile {number}",
                description="",
                installation_policy="keep-cached",
                labels={},
                favorite=False,
                definition=FleetProfileDefinition(
                    name="Default" if number == 1 else f"Profile {number}"
                ),
                assignments=[],
                fleet=fleet,
                status="not-created",
                warnings=["Profile has not been created; the first edit will save it"],
                next_actions=[
                    f"vonkctl --profile {number} profile add RECIPE --spark SPARK"
                ],
                profile_digest=_digest(document),
                created_by="uncreated",
                created_at=now,
                updated_at=now,
            )

    @staticmethod
    def _definition(row: FleetProfile) -> FleetProfileDefinition:
        """The saved authoring intent; damaged choices are retired, never refused."""

        document = {
            name: getattr(row, name) for name in FleetProfileDefinition.model_fields
        }

        def read(value: Mapping[str, object] = document) -> FleetProfileDefinition:
            return read_stored_model(
                FleetProfileDefinition, canonical_message(value), from_json=True
            )

        def rebuild() -> FleetProfileDefinition:
            retained = [
                json.loads(canonical_message(choice))
                for choice in FleetProfileService._choices(row)
            ]
            return read({**document, "assignments": retained})

        value = read_or_rebuild(
            kind="profile-definition",
            subject=str(row.id),
            read=_without_values(read),
            rebuild=_rebuild_without_values(rebuild),
        )
        return FleetProfileDefinition() if isinstance(value, Residue) else value

    def definition_number(self, number: int) -> FleetProfileDefinitionView:
        """Read authoring intent without consulting catalog, cache, or runtime."""
        if type(number) is not int or number < 1:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
            if row is None:
                return FleetProfileDefinitionView(
                    id=None,
                    number=number,
                    revision=0,
                    definition=FleetProfileDefinition(
                        name="Default" if number == 1 else f"Profile {number}"
                    ),
                )
            return FleetProfileDefinitionView(
                id=row.id,
                number=row.number,
                revision=row.revision,
                definition=self._definition(row),
            )

    def create(
        self, value: FleetProfileInput, *, actor: str, number: int | None = None
    ) -> FleetProfileView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            assignments, notes = self._validated_assignments(session, value.assignments)
            self._reserve_saved_profile_references(session, value.assignments, now=now)
            row = FleetProfile(
                number=number
                if number is not None
                else self._next_profile_number(session),
                revision=1,
                name=value.name,
                description=value.description,
                installation_policy=value.installation_policy,
                labels=dict(value.labels),
                favorite=value.favorite,
                assignments=assignments,
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise FleetProfileInvalid(
                    "a Fleet profile with this name already exists",
                    reason=InvalidRequestReason.DUPLICATE,
                ) from error
            result = self._view(session, row)
        return _with_save_notes(result, notes)

    def update(
        self, profile_id: str, value: FleetProfileInput, *, actor: str
    ) -> FleetProfileView:
        del (
            actor
        )  # Updates retain the original creator and are audited at the API boundary.
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(FleetProfile, profile_id, with_for_update=True)
            if row is None:
                raise MissingRecord(profile_id, reason=InvalidRequestReason.NOT_FOUND)
            if row.revision != value.expected_revision:
                raise FleetProfileInvalid(
                    f"profile revision conflict: expected {value.expected_revision}, current {row.revision}",
                    reason=InvalidRequestReason.CONFLICT,
                )
            assignments, notes = self._validated_assignments(session, value.assignments)
            self._reserve_saved_profile_references(session, value.assignments, now=now)
            row.name = value.name
            row.description = value.description
            row.installation_policy = value.installation_policy
            row.labels = dict(value.labels)
            row.favorite = value.favorite
            row.revision += 1
            row.assignments = assignments
            row.updated_at = now
            try:
                session.flush()
            except IntegrityError as error:
                raise FleetProfileInvalid(
                    "a Fleet profile with this name already exists",
                    reason=InvalidRequestReason.DUPLICATE,
                ) from error
            result = self._view(session, row)
        return _with_save_notes(result, notes)

    def update_number(
        self, number: int, value: FleetProfileInput, *, actor: str
    ) -> FleetProfileView:
        """Autosave a numbered profile with optimistic concurrency."""

        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
        if row is None:
            if type(number) is not int or number < 1:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            if value.expected_revision != 0:
                raise FleetProfileInvalid(
                    "profile revision conflict: profile has not been created",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return self.create(value, actor=actor, number=number)
        return self.update(row.id, value, actor=actor)

    def load(
        self,
        number: int,
        *,
        actor: str,
        request_key: str,
        reviewed_effects_digest: str | None = None,
    ) -> FleetProfileApplicationView:
        """Admit the current plan; replay before consulting mutable choices.

        A caller that showed a review names its effects digest. The plan is
        then admitted only while it still has those effects.
        """
        with self._sessions() as session:
            profile_id = session.scalar(
                select(FleetProfile.id).where(FleetProfile.number == number)
            )
        if profile_id is None:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        return self.apply(
            profile_id,
            request_key=request_key,
            actor=actor,
            reviewed_effects_digest=reviewed_effects_digest,
        )

    def progress_number(self, number: int) -> FleetProfileApplicationView:
        profile = self.get_number(number)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.profile_id == profile.id)
                .order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(1)
            )
            if row is None:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            return self._application_view(row)

    def endpoint_intent(
        self, session: Session, number: int
    ) -> FleetProfileEndpointIntent:
        """Resolve endpoint membership from the currently selected snapshot.

        The saved profile is deliberately not consulted: it may have been
        edited since the application whose workloads are currently loaded.
        The operation projection calls this with its own SQL session so the
        profile/application/run ownership read shares one database snapshot.
        """

        if type(number) is not int or number < 1:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        profile = session.scalar(
            select(FleetProfile).where(FleetProfile.number == number)
        )
        if profile is None:
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=None,
                application_id=None,
                application_state=None,
                assignments=(),
            )
        selection = session.get(FleetProfileSelection, 1)
        if selection is None or selection.profile_id != profile.id:
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=None,
                application_state=None,
                assignments=(),
            )
        application = session.get(FleetProfileApplication, selection.application_id)
        if application is None or application.profile_id != profile.id:
            # The selection names an application that is not stored: it is shown
            # as an unreadable intent (as a damaged plan is below), never refused.
            retire_as_unknown(
                "profile-selection",
                str(selection.application_id),
                BookkeepingReason.ROW_INCOMPLETE,
                "the selected profile application is not stored",
            )
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=None,
                application_state=None,
                assignments=None,
                projection_issue=FleetProfileEndpointProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail="The selected profile application is unavailable.",
                ),
            )

        application_state = _OPERATION_STATE_ADAPTER.validate_python(
            application.state, strict=True
        )
        issue_detail: str | None = None
        try:
            intended = self._intended_profile(application, session=session)
        except (FleetProfileConflict, ValidationError) as error:
            cause = error.__cause__
            issue_detail = stored_document_detail(error)
            if issue_detail is None and isinstance(cause, Exception):
                issue_detail = stored_document_detail(cause)
            intended = None
        if isinstance(intended, Residue) or intended is None:
            # Broken historical plan or progress blocks execution, but it
            # should not hide the rest of this read-only endpoint projection.
            # The exact reviewed assignments remain unknown; never reconstruct
            # them from today's mutable saved profile.
            detail = (
                _residue_detail(intended)
                if isinstance(intended, Residue)
                else issue_detail
            )
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=application.id,
                application_state=application_state,
                assignments=None,
                projection_issue=FleetProfileEndpointProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail=detail
                    or "Stored application intent is invalid or inconsistent.",
                ),
            )
        projected: list[FleetProfileEndpointAssignmentIntent] = []
        for assignment in intended.assignments:
            if assignment.desired_state == DesiredAssignmentState.INSTALLED:
                projected.append(
                    FleetProfileEndpointAssignmentIntent(
                        assignment_id=assignment.id,
                        recipe_title=assignment.recipe_title,
                        desired_state=DesiredAssignmentState.INSTALLED,
                        alias=assignment.alias,
                        state=EndpointState.INSTALLED_ONLY,
                    )
                )
                continue

            if assignment.alias is None:
                state = EndpointState.UNAVAILABLE
                run_id = None
            else:
                current = self._assignment_state(session, assignment)
                run = current.run
                if (
                    run is not None
                    and run.alias == assignment.alias
                    and run.state == RunState.RUNNING
                    and run.route_state == RouteState.PUBLISHED
                ):
                    state = EndpointState.NOT_PUBLISHED_YET
                    run_id = run.id
                elif run is not None and run.route_state == RouteState.PENDING:
                    state = EndpointState.NOT_PUBLISHED_YET
                    run_id = None
                elif run is not None and run.route_state == RouteState.FAILED:
                    state = EndpointState.UNAVAILABLE
                    run_id = None
                elif application_state == LifecycleState.SUCCEEDED:
                    state = EndpointState.WITHDRAWN
                    run_id = None
                elif application_state in {
                    LifecycleState.FAILED,
                    LifecycleState.CANCELLED,
                    LifecycleState.SUPERSEDED,
                }:
                    state = EndpointState.UNAVAILABLE
                    run_id = None
                else:
                    state = EndpointState.NOT_PUBLISHED_YET
                    run_id = None
            projected.append(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id=assignment.id,
                    recipe_title=assignment.recipe_title,
                    desired_state=DesiredAssignmentState.RUNNING,
                    alias=assignment.alias,
                    state=state,
                    expected_run_id=run_id,
                )
            )
        return FleetProfileEndpointIntent(
            number=number,
            profile_id=profile.id,
            application_id=application.id,
            application_state=application_state,
            assignments=tuple(projected),
        )

    def preview(
        self,
        profile_id: str,
        *,
        execution_assignments: tuple[FleetProfileAssignment, ...] | None = None,
        profile_name: str | None = None,
        profile_digest: str | None = None,
        accepted_intent: FleetProfileIntendedConfiguration | None = None,
        accepted_profile_revision: int | None = None,
        accepted_profile_definition: FleetProfileDefinition | None = None,
        allow_pending_cache_rebuild: bool = False,
        profile_application_id: str | None = None,
        excluded_application_id: str | None = None,
    ) -> FleetProfilePreview:
        now = _aware(self._clock())
        with self._sessions() as session:
            recovery_images: dict[str, RuntimeImageIdentity] = {}
            if profile_application_id is not None:
                recovery = session.get(FleetProfileApplication, profile_application_id)
                if recovery is None or recovery.profile_id != profile_id:
                    raise FleetProfileInvalid(
                        "Recovery review owner is unavailable",
                        reason=InvalidRequestReason.NOT_FOUND,
                    )
                reviewed = self._reviewed_profile_plan(recovery, session=session)
                # An accepted plan that cannot be read names no exact images to
                # hold the review to: it is planned against what is observed now.
                recovery_images = (
                    {}
                    if isinstance(reviewed, Residue)
                    else {
                        item.assignment_id: item.runtime_image
                        for item in reviewed.preparation_decisions
                    }
                )
            row = session.get(FleetProfile, profile_id)
            resolved_assignments: tuple[FleetProfileAssignment, ...]
            if accepted_intent is not None:
                # The accepted intent is the evidence of its own snapshot: whatever
                # the caller could not name (the assignments, the revision, the
                # name of a profile that was since deleted) is read from it, and
                # its own digest is the identity (never the caller's).
                resolved_assignments = (
                    execution_assignments
                    if execution_assignments is not None
                    else tuple(accepted_intent.assignments)
                )
                resolved_name = profile_name or (
                    row.name if row is not None else "Direct placement"
                )
                resolved_digest = accepted_intent.profile_digest
                installation_policy = accepted_intent.installation_policy
                resolved_revision = (
                    accepted_profile_revision
                    if accepted_profile_revision is not None
                    else row.revision
                    if row is not None
                    else None
                )
                resolved_definition = accepted_profile_definition
            elif row is None:
                if execution_assignments is None:
                    raise MissingRecord(
                        profile_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                resolved_assignments = execution_assignments
                resolved_name = profile_name or "Direct placement"
                resolved_digest = profile_digest or _digest(
                    {
                        "profile_id": profile_id,
                        "assignments": [
                            item.model_dump(mode="json")
                            for item in resolved_assignments
                        ],
                    }
                )
                installation_policy = "keep-cached"
                resolved_revision = None
                resolved_definition = None
            else:
                view = self._view(session, row)
                resolved_assignments = self._execution_assignments(session, row)
                resolved_name = view.name
                resolved_digest = view.profile_digest
                installation_policy = row.installation_policy
                resolved_revision = row.revision
                resolved_definition = self._definition(row)
            assignment_previews: list[FleetProfileAssignmentPreview] = []
            assignment_preparations: list[FleetProfileAssignmentPreparation] = []
            assignment_assessments: list[FleetProfileAssignmentAssessment] = []
            reasons: list[FleetProfileReason] = []
            if accepted_intent is None and row is not None:
                unreadable = self._unreadable_choice_count(row)
                if unreadable:
                    # The readable choices are planned; the reviewer sees what
                    # was left out (and the effects that follow from it).
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.CHOICES_UNREADABLE,
                            detail=(
                                f"{unreadable} saved choice(s) cannot be read and "
                                "are left out of this plan; save the profile again."
                            ),
                            severity="warning",
                        )
                    )
                resolved_ids = {item.id for item in resolved_assignments}
                for choice in self._choices(row):
                    if _choice_id(choice) in resolved_ids:
                        continue
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.RECIPE_UNAVAILABLE,
                            detail=(
                                f"Recipe {choice.recipe_selector} has no active "
                                "catalog revision; the plan waits for the catalog "
                                "sync to replace it."
                            ),
                            severity="error",
                        )
                    )
            roster = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            switch_steps: list[_PlanStepDraft] = []
            preparation_unavailable_reported = False
            # Scope is the authoritative reconciliation boundary.  An idle
            # member has no assignment and must still participate in the plan.
            target_nodes = {node.node_id for node in roster}

            control = self._control_effects(
                session,
                resolved_assignments,
                target_nodes,
                installation_policy,
                expected_images=recovery_images,
                excluded_application_id=excluded_application_id,
            )
            adapter_switch_needed = control.switch_needed
            changed_nodes = control.changed_nodes
            run_effects = control.effects.runs
            installation_effects = control.effects.installations
            reasons.extend(control.reasons)
            for assignment in resolved_assignments:
                state = control.states[assignment.id]
                preparation = None
                expected_nodes = tuple(
                    sorted(node.node_id for node in assignment.nodes)
                )
                item_reasons: list[FleetProfileReason] = []
                # Exact preparation evidence is only required when this
                # assignment must actually place the model and runtime image.
                # A profile that already matches live state legitimately has
                # nothing to prepare, so unavailable evidence stays a warning
                # there rather than blocking an otherwise idle plan.
                requires_preparation = not state.installation_ready
                active_node_ids = {node.node_id for node in roster}
                unavailable_nodes = sorted(set(expected_nodes) - active_node_ids)
                known_node_ids = set(
                    session.scalars(
                        select(AgentNode.node_id).where(
                            AgentNode.node_id.in_(unavailable_nodes)
                        )
                    )
                )
                unknown_nodes = sorted(set(unavailable_nodes) - known_node_ids)
                removed_nodes = sorted(known_node_ids)
                revision = session.get(
                    CatalogDocumentRevision, assignment.recipe_revision_id
                )
                required_count = None
                if revision is not None:
                    required_count = recipe_topology(revision.document).node_count
                if unknown_nodes:
                    item_reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.SPARK_UNAVAILABLE,
                            detail=(
                                "Assignment references Spark IDs that are not enrolled: "
                                + ", ".join(unknown_nodes)
                            ),
                            severity="error",
                        )
                    )
                if removed_nodes:
                    item_reasons.append(
                        FleetProfileReason(
                            code=(
                                ProfileReasonCode.INCOMPLETE_MULTI_SPARK_MODEL
                                if required_count is not None and required_count > 1
                                else ProfileReasonCode.SPARK_REMOVED
                            ),
                            detail=(
                                (
                                    "Multi-Spark model cannot run because the profile "
                                    "no longer has a Spark for every rank; reachable "
                                    "ranks will be stopped. Missing Sparks: "
                                )
                                if required_count is not None and required_count > 1
                                else "Assignment includes a Spark outside the live fleet and will be ignored: "
                            )
                            + ", ".join(removed_nodes),
                            severity="warning",
                        )
                    )
                if required_count is None or len(assignment.nodes) != required_count:
                    item_reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.TOPOLOGY_INCOMPLETE,
                            detail=(
                                f"Recipe requires {required_count or 'a valid'} Sparks; "
                                f"the draft assigns {len(assignment.nodes)}."
                            ),
                            severity="error",
                        )
                    )
                if unavailable_nodes:
                    # A profile still covers the current fleet.  An assignment
                    # that refers to a removed Spark is deliberately not
                    # relaunched or allowed to block the other assignments.
                    # IDs that were never enrolled remain errors above.
                    # Any reachable rank cleanup was already reviewed in the
                    # profile effects above.
                    preparation = None
                elif self._assessment_provider is None:
                    if not preparation_unavailable_reported:
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                detail=(
                                    "Exact model and OCI preparation evidence is "
                                    f"unavailable for {assignment.recipe_title}; "
                                    "the Controller has no preparation provider "
                                    "configured."
                                ),
                                severity="warning",
                            )
                        )
                        preparation_unavailable_reported = True
                else:
                    try:
                        assessment = self._assessment_provider(
                            session,
                            assignment,
                            expected_nodes,
                            allow_pending_cache_rebuild=allow_pending_cache_rebuild,
                            expected_runtime_image=recovery_images.get(assignment.id),
                            excluded_profile_application_ids=tuple(
                                item.id
                                for item in control.effects.superseded
                                if item.kind == "profile-application"
                            ),
                        )
                        if not isinstance(assessment, RunSwitchAssessment):
                            raise InvalidType(
                                assessment.note
                                if isinstance(assessment, Residue)
                                else "The planner returned an invalid assessment.",
                                reason=InvalidRequestReason.MALFORMED,
                            )
                        observed_fit_nodes = tuple(
                            sorted(
                                node.node_id for node in assessment.fit_current.nodes
                            )
                        )
                        observed_after_nodes = (
                            tuple(
                                sorted(
                                    node.node_id
                                    for node in assessment.fit_after_stop.nodes
                                )
                            )
                            if assessment.fit_after_stop is not None
                            else expected_nodes
                        )
                        if (
                            observed_fit_nodes != expected_nodes
                            or observed_after_nodes != expected_nodes
                        ):
                            raise InvalidValue(
                                "The planner assessment does not cover the exact assignment scope.",
                                reason=InvalidRequestReason.CONFLICT,
                            )
                        assignment_assessments.append(
                            FleetProfileAssignmentAssessment(
                                assignment_id=assignment.id, assessment=assessment
                            )
                        )
                        preparation = assessment.preparation
                        if (
                            preparation is None
                            and assessment.allowed
                            and allow_pending_cache_rebuild
                        ):
                            reasons.append(
                                FleetProfileReason(
                                    code=ProfileReasonCode.RUNTIME_IMAGE_REBUILD_PENDING,
                                    detail="The accepted runtime image needs cache repair before distribution.",
                                    severity="warning",
                                )
                            )
                        elif preparation is None and requires_preparation:
                            reasons.append(
                                FleetProfileReason(
                                    code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                    detail="Prepare the exact model and runtime image in the Controller cache before loading this assignment.",
                                    severity="error",
                                )
                            )
                    except (KeyError, RuntimeError, TypeError, ValueError) as error:
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                detail=str(error)[:512]
                                or "The preparation provider returned no exact evidence.",
                                severity=(
                                    "error" if requires_preparation else "warning"
                                ),
                            )
                        )
                    if preparation is not None and not isinstance(
                        preparation, RolloutPreparation
                    ):
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                detail="The preparation provider returned an invalid contract.",
                                severity=(
                                    "error" if requires_preparation else "warning"
                                ),
                            )
                        )
                        preparation = None
                    if preparation is not None:
                        observed_target_ids = tuple(preparation.target_node_ids)
                        observed_model_targets = tuple(
                            sorted(
                                target.node_id for target in preparation.model.targets
                            )
                        )
                        observed_image_targets = tuple(
                            sorted(
                                target.node_id
                                for target in preparation.runtime_image.targets
                            )
                        )
                        if (
                            observed_target_ids != expected_nodes
                            or observed_model_targets != expected_nodes
                            or observed_image_targets != expected_nodes
                        ):
                            reasons.append(
                                FleetProfileReason(
                                    code=ProfileReasonCode.PREPARATION_SCOPE_MISMATCH,
                                    detail=(
                                        "Preparation evidence does not cover exactly "
                                        f"the assignment target scope ({', '.join(expected_nodes)})."
                                    ),
                                    severity="error",
                                )
                            )
                            preparation = None
                if preparation is not None:
                    assignment_preparations.append(
                        FleetProfileAssignmentPreparation(
                            assignment_id=assignment.id,
                            preparation=preparation,
                        )
                    )
                actions: list[FleetProfileAction] = []
                if unknown_nodes:
                    actions = []
                elif state.current_state == assignment.desired_state:
                    actions.append("keep")
                else:
                    actions.append("switch")
                assignment_previews.append(
                    FleetProfileAssignmentPreview(
                        assignment_id=assignment.id,
                        recipe_revision_id=assignment.recipe_revision_id,
                        recipe_title=assignment.recipe_title,
                        desired_state=assignment.desired_state,
                        current_state=state.current_state,
                        node_ids=[node.node_id for node in assignment.nodes],
                        option_choices=dict(assignment.option_choices),
                        actions=actions,
                        reasons=item_reasons,
                    )
                )
                reasons.extend(item_reasons)

            if adapter_switch_needed:
                if not changed_nodes or not changed_nodes <= target_nodes:
                    if not any(reason.severity == "error" for reason in reasons):
                        # The effect scope observed now cannot be pinned to the
                        # roster (it changed while planning): the plan is not
                        # admissible yet, and it is planned again.
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.SWITCH_SCOPE_UNRESOLVED,
                                detail=(
                                    "The Spark scope of the profile switch changed "
                                    "while it was planned; it is planned again."
                                ),
                                severity="error",
                            )
                        )
                else:
                    switch_steps.append(
                        {
                            "kind": "switch",
                            "node_ids": sorted(changed_nodes),
                            "label": f"Switch profile {resolved_name}",
                        }
                    )
                if self._switch_adapter is None:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.SWITCH_AUTHORITY_UNAVAILABLE,
                            detail="Run/Switch authority is required to apply this profile.",
                            severity="error",
                        )
                    )
            raw_steps = switch_steps
            steps = [
                FleetProfilePlanStep(index=index, **step)
                for index, step in enumerate(raw_steps)
            ]
            # The planner owns named admission blockers, including preparation
            # failures. Its canonical assessment rejects hidden preparation
            # blockers, so count each reason once and render that same owner.
            blocker_count = sum(reason.severity == "error" for reason in reasons) + sum(
                len(item.assessment.blockers) for item in assignment_assessments
            )
            summary = FleetProfilePlanSummary(
                already_correct=sum(
                    item.actions == ["keep"] for item in assignment_previews
                ),
                placements=sum(step.kind == "create-placement" for step in steps),
                builds=sum(step.kind == "build" for step in steps),
                distributions=sum(step.kind == "distribute-image" for step in steps),
                installs=sum(
                    not state.installation_ready for state in control.states.values()
                ),
                starts=sum(
                    item.desired_state == DesiredAssignmentState.RUNNING
                    and "switch" in item.actions
                    for item in assignment_previews
                ),
                stops=sum(effect.action == "stop" for effect in run_effects),
                uninstalls=sum(
                    effect.action == "remove" for effect in installation_effects
                ),
                blockers=blocker_count,
            )
            effects = control.effects
            preparation_steps: list[FleetProfilePlanStep] = []
            if self._preparation_starter is not None:
                for assignment_preview in _assignments_needing_preparation(
                    assignment_previews,
                    {item.assignment_id for item in assignment_preparations},
                    reasons,
                    assignment_assessments,
                ):
                    title = assignment_preview.recipe_title
                    for label in (
                        f"Download the model files for {title} (skipped when cached)",
                        f"Build the runtime image for {title} (skipped when built)",
                    ):
                        preparation_steps.append(
                            FleetProfilePlanStep(
                                index=len(preparation_steps),
                                kind="prepare",
                                node_ids=list(assignment_preview.node_ids),
                                label=label,
                            )
                        )
            blocking_codes = {
                reason.code for reason in reasons if reason.severity == "error"
            } | {
                reason.code
                for item in assignment_assessments
                for reason in item.assessment.blockers
            }
            waits_for_preparation = bool(
                preparation_steps
                and blocker_count > 0
                and blocking_codes <= _PREPARATION_RESOLVABLE_CODES
            )
            ordered_preparations = sorted(
                assignment_preparations, key=lambda item: item.assignment_id
            )
            ordered_assessments = sorted(
                assignment_assessments, key=lambda item: item.assignment_id
            )
            decision = FleetProfileReviewedDecision(
                profile_id=profile_id,
                profile_name=resolved_name,
                profile_digest=resolved_digest,
                profile_revision=resolved_revision,
                profile_definition=resolved_definition,
                allowed=blocker_count == 0,
                scope=FleetProfileScopePreview(
                    node_ids=sorted(target_nodes),
                    idle_node_ids=sorted(
                        target_nodes
                        - {
                            node.node_id
                            for assignment in resolved_assignments
                            for node in assignment.nodes
                        }
                    ),
                ),
                summary=summary,
                assignments=assignment_previews,
                resolved_assignments=sorted(
                    resolved_assignments, key=lambda item: item.id
                ),
                admission_decisions=[
                    FleetProfileAdmissionDecision.from_assessment(item)
                    for item in ordered_assessments
                ],
                preparation_decisions=[
                    FleetProfilePreparationDecision.from_preparation(item)
                    for item in ordered_preparations
                ],
                effects=effects,
                steps=steps,
                preparation_steps=preparation_steps,
                waits_for_preparation=waits_for_preparation,
                reasons=reasons,
            )
            return FleetProfilePreview(
                **{
                    name: getattr(decision, name)
                    for name in type(decision).model_fields
                },
                generated_at=now,
                assessments=ordered_assessments,
                preparations=ordered_preparations,
                plan_digest=_digest(decision),
                effects_digest=_review_effects_digest(decision),
            )

    @classmethod
    def _control_effects(
        cls,
        session: Session,
        resolved_assignments: tuple[FleetProfileAssignment, ...],
        target_nodes: set[str],
        installation_policy: str,
        *,
        expected_images: Mapping[str, RuntimeImageIdentity] | None = None,
        excluded_application_id: str | None = None,
    ) -> _ProfileControlEffects:
        """Reconcile SQL-owned effects without cache, planner or external work."""
        states = {
            assignment.id: cls._assignment_state(
                session,
                assignment,
                expected_image=(expected_images or {}).get(assignment.id),
            )
            for assignment in resolved_assignments
        }
        desired_installation_ids: set[str] = set()
        desired_run_ids: set[str] = set()
        run_effects: list[FleetProfileRunEffect] = []
        installation_effects: list[FleetProfileInstallationEffect] = []
        reasons: list[FleetProfileReason] = []
        changed_nodes: set[str] = set()
        unavailable_assignment_ids: set[str] = set()
        adapter_switch_needed = False
        for assignment in resolved_assignments:
            state = states[assignment.id]
            unavailable_assignment_ids.update(
                (assignment.id,)
                if {node.node_id for node in assignment.nodes} - target_nodes
                else ()
            )
            if state.installation is not None:
                desired_installation_ids.add(state.installation.id)
                installation_effects.append(
                    FleetProfileInstallationEffect(
                        installation_id=state.installation.id,
                        node_ids=list(
                            cls._installation_node_ids(session, state.installation.id)
                        ),
                        action="keep",
                    )
                )
            if (
                state.run is not None
                and assignment.desired_state == DesiredAssignmentState.RUNNING
                and state.current_state == ObservedAssignmentState.RUNNING
            ):
                desired_run_ids.add(state.run.id)
            if state.current_state != assignment.desired_state:
                assignment_targets = {
                    node.node_id for node in assignment.nodes
                } & target_nodes
                if assignment_targets:
                    adapter_switch_needed = True
                    changed_nodes.update(assignment_targets)

        # Every active run intersecting scope is reconciled to the desired
        # running set, independent of installation retention policy.  A
        # distributed run that crosses the boundary is a hard blocker: the
        # controller must never stop only the in-scope ranks.
        active_runs = tuple(
            session.scalars(
                select(RecipeRun)
                .where(RecipeRun.state.in_(STOPPABLE_RUN_STATES))
                .order_by(RecipeRun.created_at, RecipeRun.id)
            )
        )
        run_nodes = cls._run_nodes(session, [run.id for run in active_runs])
        if not resolved_assignments:
            # An empty assignment set is an explicit all-idle outcome.  If
            # the scope currently contains a run, route reconciliation
            # through the composite child so Run/Switch can stop the
            # complete distributed group exactly once.
            for run in active_runs:
                members = set(run_nodes.get(run.id, ()))
                members.update(cls._installation_node_ids(session, run.installation_id))
                if members & target_nodes:
                    adapter_switch_needed = True
                    changed_nodes.update(members & target_nodes)
            # A queued workload may not have created a Run yet.  An
            # explicit all-idle profile still has cancellation work in
            # that case; the adapter waits for issued cancellation receipts
            # before publishing its final no-workload receipt.
            for pending in session.scalars(
                select(Job).where(
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    )
                )
            ):
                if type(pending.payload.get("workload_intent_ordinal")) is not int:
                    continue
                members = set(pending.targets)
                if not members & target_nodes:
                    continue
                if not members <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.PENDING_CROSS_SCOPE,
                            detail="A pending workload crosses the selected idle scope.",
                            severity="error",
                        )
                    )
                    continue
                adapter_switch_needed = True
                changed_nodes.update(members)
            for pending in session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                )
            ):
                if pending.id == excluded_application_id:
                    continue
                if _persisted_profile_progress(pending).admission_pending:
                    continue
                _pending_plan = _persisted_profile_plan(pending)
                if isinstance(_pending_plan, Residue):
                    # The step list is unreadable, so the declared frozen
                    # scope is the only durable authority left for which
                    # nodes this order can still affect.  Never infer a
                    # narrower cleanup scope from a damaged document.
                    scope = _persisted_profile_scope(pending)
                    if scope is None:
                        # An unreadable record never blocks new work.
                        warn_unreadable_once("profile application", pending.id)
                        continue
                    members = set(scope)
                else:
                    members = {
                        node_id
                        for step in _pending_plan.steps
                        for node_id in step.node_ids
                    }
                if not members & target_nodes:
                    # An unrelated damaged record must not veto a fresh
                    # authorized profile; it stays queued for its own
                    # worker to quarantine.
                    continue
                if not members <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.PENDING_CROSS_SCOPE,
                            detail="A pending profile change crosses the selected idle scope.",
                            severity="error",
                        )
                    )
                    continue
                adapter_switch_needed = True
                changed_nodes.update(members)
        for run in active_runs:
            members = set(run_nodes.get(run.id, ()))
            # Installation membership is the authoritative complete
            # placement even when a partial observation omitted a rank.
            members.update(cls._installation_node_ids(session, run.installation_id))
            intersection = members & target_nodes
            if not intersection:
                continue
            profile_stop_scope = None
            missing_members = members - target_nodes
            if missing_members:
                mapping_members = tuple(
                    session.scalars(
                        select(ClusterMappingNode)
                        .where(ClusterMappingNode.mapping_id == run.mapping_id)
                        .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
                    )
                )
                active_missing = tuple(
                    session.scalars(
                        select(AgentNode).where(
                            AgentNode.node_id.in_(missing_members),
                            AgentNode.revoked_at.is_(None),
                        )
                    )
                )
                if (
                    not intersection
                    or len(members) < 2
                    or {node.node_id for node in mapping_members} != members
                    or active_missing
                ):
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.DISTRIBUTED_CROSS_SCOPE,
                            detail=(
                                f"Running workload {run.alias} uses Sparks outside "
                                "the profile scope; review the complete distributed group."
                            ),
                            severity="error",
                        )
                    )
                    continue
                original_group = SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node.node_id,
                            rank=node.rank,
                            role=node.role,
                            endpoint_owner=node.endpoint_owner,
                        )
                        for node in mapping_members
                    ]
                )
                profile_stop_scope = RunSwitchProfileStopScope(
                    original_group=original_group,
                    target_node_ids=sorted(intersection),
                    missing_node_ids=sorted(missing_members),
                )
            run_effects.append(
                FleetProfileRunEffect(
                    run_id=run.id,
                    installation_id=run.installation_id,
                    alias=run.alias,
                    node_ids=sorted(members),
                    action="keep" if run.id in desired_run_ids else "stop",
                    profile_stop_scope=profile_stop_scope
                    if run.id not in desired_run_ids
                    else None,
                )
            )
            if run_effects[-1].action == "stop":
                adapter_switch_needed = True
                changed_nodes.update(intersection)

        if installation_policy == "exact" and target_nodes:
            installations = tuple(
                session.scalars(
                    select(RecipeInstallation)
                    .where(RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES))
                    .order_by(RecipeInstallation.created_at, RecipeInstallation.id)
                )
            )
            installation_nodes = cls._installation_nodes(
                session, [item.id for item in installations]
            )
            for installation in installations:
                nodes = installation_nodes.get(installation.id, ())
                node_ids = {node.node_id for node in nodes}
                if (
                    not node_ids.intersection(target_nodes)
                    or installation.id in desired_installation_ids
                ):
                    continue
                if not node_ids <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.SHARED_INSTALLATION_SCOPE,
                            detail="Exact installation policy would affect a multi-Spark installation outside the profile scope.",
                            severity="error",
                        )
                    )
                    continue
                # An all-idle exact profile still owns removal of scoped
                # stopped residue. Without a switch step the adapter never
                # receives this desired retention decision.
                adapter_switch_needed = True
                changed_nodes.update(node_ids)
                installation_effects.append(
                    FleetProfileInstallationEffect(
                        installation_id=installation.id,
                        node_ids=sorted(node_ids),
                        action="remove",
                    )
                )
                reasons.append(
                    FleetProfileReason(
                        code=ProfileReasonCode.CLEANUP_DELEGATED,
                        detail=(
                            "Run/Switch removes installation "
                            f"{installation.id} under this profile's "
                            "retention policy."
                        )[:512],
                        severity="info",
                    )
                )

        if adapter_switch_needed and active_runs:
            reasons.append(
                FleetProfileReason(
                    code=ProfileReasonCode.INTERRUPTION_EXPECTED,
                    detail=(
                        "The reviewed plan includes required runtime stops; "
                        "affected workloads may be unavailable until final starts complete."
                    ),
                    severity="warning",
                )
            )

        return _ProfileControlEffects(
            states=states,
            effects=FleetProfileEffects(
                runs=sorted(run_effects, key=lambda effect: effect.run_id),
                installations=sorted(
                    installation_effects, key=lambda effect: effect.installation_id
                ),
                superseded=cls._pending_effects(
                    session,
                    changed_nodes,
                    excluded_application_id=excluded_application_id,
                ),
            ),
            changed_nodes=changed_nodes,
            unavailable_assignment_ids=unavailable_assignment_ids,
            switch_needed=adapter_switch_needed,
            reasons=reasons,
        )

    @staticmethod
    def _pending_effects(
        session: Session,
        changed_nodes: set[str],
        *,
        excluded_application_id: str | None = None,
    ) -> list[FleetProfilePendingEffect]:
        """Identify older orders whose effects the new decision supersedes."""
        effects: list[FleetProfilePendingEffect] = []
        if not changed_nodes:
            return effects
        for pending in session.scalars(
            select(Job).where(
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                )
            )
        ):
            if type(pending.payload.get("workload_intent_ordinal")) is not int:
                continue
            members = set(pending.targets)
            if members & changed_nodes:
                effects.append(
                    FleetProfilePendingEffect(
                        kind="job", id=pending.id, node_ids=sorted(members)
                    )
                )
        for pending in session.scalars(
            select(FleetProfileApplication).where(
                FleetProfileApplication.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                ),
            )
        ):
            if pending.id == excluded_application_id:
                continue
            # A parked receipt is not accepted selection authority, but a
            # fresh reviewed load still needs to show that accepting it
            # will retire the older overlapping receipt.
            plan = _persisted_profile_plan(pending)
            if isinstance(plan, Residue):
                # A damaged order retains only its readable frozen authority.
                # Preview's existing blocker handles an unreadable scope.
                members = set(_persisted_profile_scope(pending) or ())
            else:
                members = {node_id for step in plan.steps for node_id in step.node_ids}
            if members & changed_nodes:
                effects.append(
                    FleetProfilePendingEffect(
                        kind="profile-application",
                        id=pending.id,
                        node_ids=sorted(members),
                    )
                )
        return sorted(effects, key=lambda effect: (effect.kind, effect.id))

    def _matching_application(
        self,
        row: FleetProfileApplication,
        *,
        session: Session,
        profile_id: str,
        reviewed_digest: str | None,
        actor: str,
        retry_of_application_id: str | None = None,
    ) -> FleetProfileApplicationView:
        progress = _persisted_profile_progress(row)
        intended = self._intended_profile(row, session=session)
        if (
            row.profile_id != profile_id
            or row.actor != actor
            or progress.retry_of_application_id != retry_of_application_id
            or (
                reviewed_digest is not None
                and not isinstance(intended, Residue)
                and intended.reviewed_plan_digest != reviewed_digest
            )
        ):
            raise FleetProfileInvalid(
                "Fleet profile request key was reused for another plan",
                reason=InvalidRequestReason.CONFLICT,
            )
        return self._application_view(row)

    def _load_replay(
        self,
        profile_id: str,
        *,
        request_key: str,
        actor: str,
    ) -> FleetProfileApplicationView | None:
        with self._sessions.begin() as session:
            self._authorize(session, actor)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            return (
                self._matching_application(
                    existing,
                    session=session,
                    profile_id=profile_id,
                    reviewed_digest=None,
                    actor=actor,
                )
                if existing is not None
                else None
            )

    def _create_pending_application(
        self,
        preview: FleetProfilePreview,
        *,
        request_key: str,
        actor: str,
        operation_kind: FleetProfileOperationKind,
        select_profile: bool = False,
        selection_precondition: _SelectedProfileSnapshot | None = None,
    ) -> FleetProfileApplicationView:
        """Persist reviewed intent before admission locks are reacquired.

        The row owns no workload ordinal, reservation, or child effect until
        the normal admission transaction binds it.  This lets the reconciler
        retry after a transient owner clears without holding a SQL lock or
        asking the operator to resubmit the same reviewed request.
        """

        now = _aware(self._clock())
        application_id = str(uuid.uuid4())
        # The application identity is unique even while admission is parked.
        # Bind that execution identity before the first durable insert so two
        # concurrent reviewed loads cannot collide on the unique plan digest,
        # and so the receipt cannot change identity after a caller observes it.
        pending_plan_digest = _digest(
            {
                "schema_version": 2,
                "reconciliation_digest": preview.plan_digest,
                "retry_of_application_id": None,
                "request_key": request_key,
            }
        )
        pending_preview = preview.model_copy(
            update={"plan_digest": pending_plan_digest}
        )
        with self._sessions.begin() as session:
            self._authorize(session, actor)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if existing is not None:
                return self._matching_application(
                    existing,
                    session=session,
                    profile_id=preview.profile_id,
                    reviewed_digest=preview.plan_digest,
                    actor=actor,
                )
            profile = session.get(FleetProfile, preview.profile_id)
            if profile is None:
                raise MissingRecord(
                    preview.profile_id, reason=InvalidRequestReason.NOT_FOUND
                )
            if selection_precondition is None:
                if _digest(_profile_document(profile)) != preview.profile_digest:
                    raise FleetProfileStalePlanConflict(
                        "Fleet profile changed before its admission intent was persisted"
                    )
                installation_policy = _INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                )
            else:
                current = self._selected_profile_snapshot(session)
                if (
                    not select_profile
                    or current is None
                    or isinstance(current, Residue)
                    or current.generation != selection_precondition.generation
                    or current.application_id != selection_precondition.application_id
                    or current.roster_digest != selection_precondition.roster_digest
                    or selection_precondition.profile_id != preview.profile_id
                    or preview.profile_digest
                    != selection_precondition.intended.profile_digest
                    or preview.profile_revision
                    != selection_precondition.profile_revision
                ):
                    raise FleetProfileStalePlanConflict(
                        "Selected profile or fleet membership changed during reconciliation"
                    )
                installation_policy = (
                    selection_precondition.intended.installation_policy
                )
            intended = FleetProfileIntendedConfiguration(
                profile_digest=preview.profile_digest,
                reviewed_plan_digest=preview.plan_digest,
                reviewed_application_id=application_id,
                installation_policy=installation_policy,
                scope=FleetProfileScope(node_ids=list(preview.scope.node_ids)),
                assignments=list(preview.resolved_assignments),
            )
            created_at = _next_profile_acceptance_time(session, now)
            next_retry = FleetProfileAdapter.next_retry(application_id, 1, now)
            row = FleetProfileAdapter.new_application(
                id=application_id,
                request_key=request_key,
                profile_id=preview.profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=pending_plan_digest,
                # Keep the short synchronous admission attempt visible as a
                # normal queued application.  If it cannot bind, the defer
                # path records the next automatic attempt before returning.
                state="queued",
                plan=pending_preview.model_dump(mode="json"),
                selection_generation=(
                    selection_precondition.generation
                    if selection_precondition is not None
                    else None
                ),
                current_step=0,
                current_operation_id=None,
                progress=FleetProfileApplicationProgress(
                    operation_kind=operation_kind,
                    admission_pending=True,
                    admission_attempt=0,
                    admission_retry_at=next_retry,
                    intended_profile=intended,
                    completed_steps=0,
                    total_steps=len(preview.steps),
                ).model_dump(mode="json"),
                result=None,
                status_reason=(
                    "Profile admission is busy; reviewed intent was accepted and "
                    f"will retry automatically after {next_retry.isoformat()}."
                )[:512],
                actor=actor,
                created_at=created_at,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            # Pending applications are not authority. In particular, keeping
            # the prior selection in place makes failed admission cleanup FK-safe.
            return self._application_view(row)

    def _defer_pending_application(
        self,
        application_id: str,
        reason: str,
        *,
        retry_delay: timedelta | None = None,
        blockers: Sequence[OperationBlocker] | None = None,
        code: str = ProfileReasonCode.ADMISSION_BUSY,
        storage: FleetProfileAdmissionEffectBusy | None = None,
        wait_for_space: bool = False,
    ) -> FleetProfileApplicationView:
        """Record bounded retry state after a nonblocking admission refusal.

        A refusal for disk (``storage``) asks the storage collector to free it
        and records what that can do: the wait is bounded and ends in a typed
        refusal when nothing more can be freed or the space does not come.

        ``wait_for_space`` is the load whose plan is still blocked (by disk
        among other things): space may also appear without eviction (a stop, a
        removal), so it keeps waiting while the bound allows and only then ends
        with the reason that holds the space, or ``storage.eviction_timed_out``.
        """

        now = _aware(self._clock())
        relieved: list[tuple[str, StorageRelief]] = []
        if storage is not None:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                profile_id = row.profile_id if row is not None else application_id
            for node_id, needed in storage.shortfalls:
                found = self._ask_storage_relief(node_id, needed, profile_id)
                if found is not None:
                    relieved.append((node_id, found))
            if relieved:
                relief_blockers = [
                    _relief_blocker(node_id, item) for node_id, item in relieved
                ]
                if blockers is None or not wait_for_space:
                    blockers = relief_blockers
                else:
                    # The plan's other blockers stay; the disk ones are replaced
                    # by what the collector just said about them.
                    blockers = [
                        item for item in blockers if item.code not in _STORAGE_CODES
                    ] + relief_blockers
            retry_delay = timedelta(seconds=STORAGE_ADMISSION_RETRY_SECONDS)
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(row)
            if not _owns_pending_admission(row, progress):
                return self._application_view(row)
            if storage is not None:
                since = _aware(progress.storage_wait_since or now)
                final = next(
                    (
                        (node_id, item)
                        for node_id, item in relieved
                        if item.code == STORAGE_INSUFFICIENT
                        and (wait_for_space or not item.paused)
                    ),
                    None,
                )
                expired = now - since > timedelta(
                    seconds=STORAGE_ADMISSION_WAIT_SECONDS
                )
                if wait_for_space and not expired:
                    final = None
                if final is not None or expired:
                    # Nothing more can be freed (or the space did not come in
                    # time): end with the reason, not another silent retry.
                    node_id, item = final or (
                        storage.shortfalls[0][0],
                        relieved[0][1] if relieved else None,
                    )
                    waited = (
                        f"Disk did not free within {STORAGE_ADMISSION_WAIT_SECONDS}s"
                        " of waiting for eviction. "
                    )
                    detail = (
                        item.detail
                        if final is not None and item is not None
                        else waited
                        + (item.detail if item is not None else str(storage))
                    )
                    refusal = make_blocker(
                        STORAGE_INSUFFICIENT
                        if final is not None
                        else STORAGE_EVICTION_TIMED_OUT,
                        detail,
                        severity="error",
                        node_ids=(node_id,),
                    )
                    row.progress = _progress_with_blockers(
                        progress,
                        [refusal],
                        admission_pending=False,
                        admission_retry_at=None,
                        storage_wait_since=since.isoformat(),
                    )
                    _LOGGER.warning(
                        "profile application %s refused for disk: %s: %s",
                        application_id,
                        refusal.code,
                        refusal.detail,
                    )
                    self._lifecycle.fail(
                        row, f"{refusal.code}: {refusal.detail}", now, session=session
                    )
                    return self._application_view(row)
                wait_changes: dict[str, object] = {
                    "storage_wait_since": since.isoformat()
                }
            else:
                wait_changes = {"storage_wait_since": None}
            attempt = progress.admission_attempt + 1
            next_retry = (
                FleetProfileAdapter.next_retry(application_id, attempt, now)
                if retry_delay is None
                else now + retry_delay
            )
            current_blockers = (
                list(blockers) if blockers is not None else [make_blocker(code, reason)]
            )
            row.progress = _progress_with_blockers(
                progress,
                current_blockers,
                admission_pending=True,
                admission_attempt=attempt,
                admission_retry_at=next_retry.isoformat(),
                **wait_changes,
            )
            if {(item.code, tuple(item.node_ids)) for item in current_blockers} != {
                (item.code, tuple(item.node_ids)) for item in progress.blockers
            }:
                _LOGGER.log(
                    logging.WARNING
                    if any(item.severity == "error" for item in current_blockers)
                    else logging.INFO,
                    "profile application %s is waiting to be admitted: %s; "
                    "next attempt %s",
                    application_id,
                    "; ".join(
                        f"{item.code}: {item.detail}" for item in current_blockers[:4]
                    ),
                    next_retry.isoformat(),
                )
            # Admission retries itself at ``admission_retry_at``; no operator
            # action is required, so the row stays queued with its next due time.
            self._lifecycle.project(
                row,
                now,
                state=_LifecycleState.QUEUED,
                reason=f"{reason} Next attempt: {next_retry.isoformat()}.",
                session=session,
            )
            session.flush()
            return self._application_view(row)

    def _discard_pending_application(self, application_id: str) -> None:
        """Delete only a still-owned pending receipt after submitter failure."""

        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return
            progress = _persisted_profile_progress(row)
            if _owns_pending_admission(row, progress):
                session.delete(row)

    def _prepare_pending_admission(
        self, application_id: str, plan: FleetProfilePreview
    ) -> None:
        """Fence older workload effects before retrying the full admission lock."""

        if self._switch_adapter is None:
            return
        execution_nodes = tuple(
            sorted({node_id for step in plan.steps for node_id in step.node_ids})
        )
        if not execution_nodes:
            return
        now = _aware(self._clock())
        try:
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication,
                    application_id,
                    with_for_update={"nowait": True},
                )
                if row is None:
                    return
                progress = _persisted_profile_progress(row)
                if not _owns_pending_admission(row, progress):
                    return
                # A persisted request may outlive the authority that accepted
                # it. Recheck that authority before fencing workloads or
                # recording cancellation against older operations.
                self._authorize(session, row.actor)
                profile = session.get(FleetProfile, row.profile_id)
                if profile is None:
                    raise MissingRecord(
                        row.profile_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                if row.selection_generation is not None:
                    progress = _persisted_profile_progress(row)
                    if not self._application_is_current_selection(
                        session, row, progress
                    ):
                        raise _FleetProfileSupersededIntentConflict(
                            "Selected Fleet profile was replaced before admission resumed"
                        )
                else:
                    self._validate_draft_review_identity(
                        session,
                        profile,
                        profile_digest=plan.profile_digest,
                        assignments=tuple(plan.resolved_assignments),
                    )
                acquire_admission_keys(
                    session,
                    tuple(node_admission_key(node_id) for node_id in execution_nodes),
                    holder="profile-workload-fence",
                )
                nodes = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.node_id.in_(execution_nodes))
                        .order_by(AgentNode.node_id)
                        .with_for_update(nowait=True)
                    )
                )
                if tuple(node.node_id for node in nodes) != execution_nodes:
                    raise FleetProfileStalePlanConflict(
                        "Pending profile workload scope changed before fencing"
                    )
                if _newer_profile_intent_overlaps(
                    session,
                    _application_order_key(session, row),
                    set(execution_nodes),
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Pending profile intent was superseded by a later accepted request"
                    )
                ordinal = progress.workload_intent_ordinal
                if ordinal is None:
                    ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
                    for node in nodes:
                        node.workload_intent_ordinal = ordinal
                elif any(node.workload_intent_ordinal != ordinal for node in nodes):
                    raise _FleetProfileSupersededIntentConflict(
                        "Pending profile workload intent was superseded before recovery"
                    )
                self._switch_adapter.request_superseded_workload_cancellation_in_session(
                    session, execution_nodes, ordinal, now
                )
                # Retire older parked profile applications while the newer
                # intent owns the node fence. Waiting applications have no
                # issued effects; leaving them eligible lets their retry loop
                # contend with the newer intent and starve admission.
                for prior in session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.id != application_id,
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                    )
                    .order_by(FleetProfileApplication.id)
                    .with_for_update(nowait=True)
                ):
                    prior_progress = _persisted_profile_progress(prior)
                    prior_plan = _persisted_profile_plan(prior)
                    if isinstance(prior_plan, Residue):
                        continue
                    prior_scope = {
                        node_id
                        for step in prior_plan.steps
                        for node_id in step.node_ids
                    }
                    if not prior_scope & set(execution_nodes):
                        continue
                    if (
                        _owns_pending_admission(prior, prior_progress)
                        and prior_progress.intended_profile is not None
                    ):
                        # Pending receipts can be inserted without taking the
                        # node fence, so check ordering again after locking the
                        # row. Retire older unbound receipts even when they
                        # never received an ordinal.
                        try:
                            prior_order = _application_order_key(session, prior)
                        except FleetProfileConflict:
                            # Broken retry lineage cannot grant a receipt
                            # ordering authority. Preserve the pre-existing
                            # ordinal rule for malformed siblings instead.
                            prior_order = None
                        if (
                            prior_order is not None
                            and prior_order > _application_order_key(session, row)
                        ):
                            raise _FleetProfileSupersededIntentConflict(
                                "A later accepted profile intent owns the workload scope"
                            )
                        if prior_order is None:
                            prior_ordinal = prior_progress.workload_intent_ordinal
                            if prior_ordinal is None or prior_ordinal >= ordinal:
                                continue
                        self._lifecycle.supersede(
                            prior,
                            "Profile order was replaced before admission by a later "
                            "scoped intent",
                            now,
                            code=SupersedeCode.SUPERSEDED_BY_INTENT,
                            by=application_id,
                            session=session,
                        )
                        continue
                    prior_ordinal = prior_progress.workload_intent_ordinal
                    if prior_ordinal is None or prior_ordinal >= ordinal:
                        continue
                    self._lifecycle.supersede(
                        prior,
                        "Profile order was replaced before admission by a later "
                        "scoped intent",
                        now,
                        code=SupersedeCode.SUPERSEDED_BY_INTENT,
                        by=application_id,
                        session=session,
                    )
                progress_data = progress.model_dump(mode="json")
                if progress.workload_intent_ordinal is None:
                    progress_data["workload_intent_ordinal"] = ordinal
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                row.updated_at = now
        except AdmissionLockBusy as error:
            raise FleetProfileAdmissionEffectBusy(
                "Profile admission is waiting for the active workload owner to finish; the Controller will retry automatically."
            ) from error
        except OperationalError as error:
            if is_admission_contention(error):
                raise FleetProfileAdmissionEffectBusy(
                    "Profile admission is waiting for the active workload owner to finish; the Controller will retry automatically."
                ) from None
            raise

    def apply(
        self,
        profile_id: str,
        *,
        request_key: str,
        actor: str,
        reviewed_effects_digest: str | None = None,
    ) -> FleetProfileApplicationView:
        replay = self._load_replay(profile_id, request_key=request_key, actor=actor)
        if replay is not None:
            return replay
        pending: FleetProfileApplicationView | None = None
        try:
            preview = self.preview(profile_id)
            if (
                reviewed_effects_digest is not None
                and preview.effects_digest != reviewed_effects_digest
            ):
                # Refused before anything is persisted: the caller reviews the
                # current plan and asks again, with the same or a new key.
                raise FleetProfileReviewStale(
                    "The profile plan changed since it was reviewed; review the "
                    "current plan and load again."
                )
            if not preview.allowed:
                if not _profile_preview_is_waitable(preview):
                    raise FleetProfileInvalid(
                        "Fleet profile intent contains a security or contract blocker",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                pending = self._create_pending_application(
                    preview,
                    request_key=request_key,
                    actor=actor,
                    operation_kind="fleet-profile.apply",
                    select_profile=True,
                )
                blockers = _preview_blockers(preview) + self._request_preparations(
                    preview, actor=actor
                )
                return self._defer_pending_application(
                    pending.id,
                    "Waiting for current Fleet conditions: "
                    + "; ".join(item.code for item in blockers[:8])
                    + ".",
                    blockers=blockers,
                    storage=_storage_wait_of_preview(preview),
                    wait_for_space=True,
                )
            # A review that planned an eviction is admitted: start it now.
            self._request_storage(preview)
            pending = self._create_pending_application(
                preview,
                request_key=request_key,
                actor=actor,
                operation_kind="fleet-profile.apply",
                select_profile=True,
            )
            for retry_delay in (
                *_PROFILE_ADMISSION_RETRY_DELAYS_SECONDS,
                None,
            ):
                try:
                    return self._queue_application(
                        preview,
                        request_key=request_key,
                        actor=actor,
                        operation_kind="fleet-profile.apply",
                        pending_application_id=pending.id,
                    )
                except FleetProfileAdmissionBusy as busy:
                    if retry_delay is None:
                        return self._defer_pending_application(
                            pending.id,
                            "Profile admission is busy"
                            + (
                                f" ({busy.holder} holds a selected Spark)"
                                if busy.holder
                                else ""
                            )
                            + "; the Controller will retry automatically.",
                            retry_delay=timedelta(0),
                        )
                    time.sleep(retry_delay)
                except (
                    FleetProfileAdmissionEffectBusy,
                    FleetProfileAdmissionStorageError,
                ) as error:
                    return self._defer_pending_application(
                        pending.id,
                        str(error),
                        retry_delay=timedelta(seconds=60)
                        if isinstance(error, FleetProfileAdmissionStorageError)
                        else timedelta(0),
                        code=_deferral_code(error),
                        storage=_storage_wait_of(error),
                    )
            # The last attempt (no delay left) defers and returns above; a spent
            # schedule is deferred the same way, to be retried automatically.
            assert pending is not None
            return self._defer_pending_application(
                pending.id,
                "Profile admission is busy; the Controller will retry automatically.",
                retry_delay=timedelta(0),
            )
        except (
            FleetProfileConflict,
            FleetProfilePermissionDenied,
            KeyError,
        ) as error:
            if pending is not None:
                if isinstance(error, _FleetProfileSupersededIntentConflict):
                    self._finish_pending_admission(
                        pending.id,
                        state=LifecycleState.SUPERSEDED,
                        reason=str(error),
                        code=SupersedeCode.SUPERSEDED_BY_INTENT,
                    )
                else:
                    self._discard_pending_application(pending.id)
            if isinstance(error, FleetProfilePermissionDenied):
                raise
            # Another identical submission can commit after our first lookup.
            # Its accepted receipt wins over a newly stale preview or a busy
            # admission boundary; this read never refreshes the approved intent.
            replay = self._load_replay(profile_id, request_key=request_key, actor=actor)
            if replay is not None:
                if replay.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                    self._discard_pending_application(replay.id)
                    raise
                return replay
            raise

    def _queue_application(
        self,
        preview: FleetProfilePreview,
        *,
        request_key: str,
        actor: str,
        operation_kind: FleetProfileOperationKind,
        retry_of_application_id: str | None = None,
        automatic_cache_recovery: bool = False,
        pending_application_id: str | None = None,
    ) -> FleetProfileApplicationView:
        now = _aware(self._clock())
        reviewed_plan_digest = preview.plan_digest
        application_id = pending_application_id or str(uuid.uuid4())
        # (An executor that is not bound yet is not a reason to refuse: the load is
        # accepted and queued, and its worker issues the steps once it is bound.)
        # The preview identifies the work; the request identifies its execution.
        # The same reconciliation can be needed again after a failure or drift.
        preview = preview.model_copy(
            update={
                "plan_digest": _digest(
                    {
                        "schema_version": 2,
                        "reconciliation_digest": preview.plan_digest,
                        "retry_of_application_id": retry_of_application_id,
                        "request_key": request_key,
                    }
                )
            }
        )
        with self._admission_session(actor, node_ids=preview.scope.node_ids) as session:
            profile = session.get(
                FleetProfile, preview.profile_id, with_for_update={"nowait": True}
            )
            if profile is None:
                raise MissingRecord(
                    preview.profile_id, reason=InvalidRequestReason.NOT_FOUND
                )
            existing = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.request_key == request_key)
                .with_for_update(nowait=True)
            )
            existing_progress = (
                _persisted_profile_progress(existing) if existing is not None else None
            )
            pending_roster_reconciliation = bool(
                existing is not None
                and existing_progress is not None
                and existing_progress.admission_pending
                and existing.selection_generation is not None
            )
            selected_application = bool(
                existing is not None and existing.selection_generation is not None
            )
            retry_parent: FleetProfileApplication | None = None
            retry_parent_progress: FleetProfileApplicationProgress | None = None
            selected_generation: int | None = None
            selected_application_id: str | None = None
            selected_roster_digest: str | None = None
            if retry_of_application_id is not None:
                retry_parent = session.get(
                    FleetProfileApplication,
                    retry_of_application_id,
                    with_for_update={"nowait": True},
                )
                if retry_parent is None:
                    raise MissingRecord(
                        retry_of_application_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                retry_parent_progress = _persisted_profile_progress(retry_parent)
                selected_application = selected_application or (
                    retry_parent_progress.intended_profile is not None
                    and self._application_is_current_selection(
                        session, retry_parent, retry_parent_progress
                    )
                )
            pending_ordinal: int | None = None
            created_at = (
                _next_profile_acceptance_time(session, now) if existing is None else now
            )
            if existing is not None:
                pending = (
                    pending_application_id == existing.id
                    and existing_progress is not None
                    and existing_progress.admission_pending
                )
                if pending and existing_progress is not None:
                    pending_ordinal = existing_progress.workload_intent_ordinal
                if pending and existing.state in {"cancelled", "superseded"}:
                    raise FleetProfileInvalid(
                        "Profile application was superseded by a later intent",
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                if not pending:
                    return self._matching_application(
                        existing,
                        session=session,
                        profile_id=preview.profile_id,
                        reviewed_digest=reviewed_plan_digest
                        if retry_of_application_id is None
                        else None,
                        actor=actor,
                        retry_of_application_id=retry_of_application_id,
                    )
            if selected_application:
                selection = session.get(
                    FleetProfileSelection, 1, with_for_update={"nowait": True}
                )
                selection_source = (
                    session.get(FleetProfileApplication, selection.application_id)
                    if pending_roster_reconciliation and selection is not None
                    else existing
                    if existing is not None
                    and existing.selection_generation is not None
                    else retry_parent
                )
                selection_source_progress = (
                    _persisted_profile_progress(selection_source)
                    if selection_source is not None and selection_source is not existing
                    else existing_progress
                    if selection_source is existing
                    else retry_parent_progress
                )
                if (
                    selection is None
                    or selection_source is None
                    or selection_source_progress is None
                    or (
                        pending_roster_reconciliation
                        and existing is not None
                        and existing.selection_generation != selection.generation
                    )
                    or not self._application_is_current_selection(
                        session, selection_source, selection_source_progress
                    )
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Selected Fleet profile was replaced by a newer load"
                    )
                selected_generation = selection.generation
                selected_application_id = selection.application_id
                selected_roster_digest = selection.roster_digest
            # Use the reviewed snapshot. Resolving through the cache here both
            # substituted newer choices and performed storage work under SQL locks.
            frozen_assignments = tuple(preview.resolved_assignments)
            if not selected_application:
                self._validate_draft_review_identity(
                    session,
                    profile,
                    profile_digest=preview.profile_digest,
                    assignments=frozen_assignments,
                )
            self._reserve_preview_assets(session, preview, now=now)
            stop_ids = tuple(
                sorted(
                    {
                        effect.run_id
                        for effect in preview.effects.runs
                        if effect.action == "stop"
                    }
                )
            )
            if stop_ids:
                # A reviewed replacement owns the exact runs and run-node rows
                # it is about to stop. Lock them before the roster snapshot so
                # another owner cannot change a reviewed effect mid-admission.
                tuple(
                    session.scalars(
                        select(RecipeRun)
                        .where(RecipeRun.id.in_(stop_ids))
                        .order_by(RecipeRun.id)
                        .with_for_update(nowait=True)
                    )
                )
                tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id.in_(stop_ids))
                        .order_by(RunNode.run_id, RunNode.rank)
                        .with_for_update(nowait=True)
                    )
                )
            scope_nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                    .with_for_update(nowait=True)
                )
            )
            frozen_nodes = tuple(node.node_id for node in scope_nodes)
            if frozen_nodes != tuple(preview.scope.node_ids):
                raise FleetProfileStalePlanConflict(
                    "Profile fleet scope changed during admission; review again"
                )
            if selected_application:
                intended = (
                    existing_progress.intended_profile
                    if existing_progress is not None
                    else retry_parent_progress.intended_profile
                    if retry_parent_progress is not None
                    else None
                )
                if (
                    intended is None
                    or intended.profile_digest != preview.profile_digest
                    or tuple(intended.scope.node_ids) != frozen_nodes
                    or tuple(intended.assignments) != tuple(frozen_assignments)
                    or (
                        retry_of_application_id is None
                        and intended.reviewed_plan_digest != reviewed_plan_digest
                    )
                    or (
                        retry_of_application_id is None
                        and intended.reviewed_application_id != application_id
                    )
                ):
                    raise FleetProfileUnavailable(
                        "Selected profile differs from its accepted snapshot",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                installation_policy = intended.installation_policy
            else:
                installation_policy = _INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                )
                intended = FleetProfileIntendedConfiguration(
                    profile_digest=preview.profile_digest,
                    reviewed_plan_digest=reviewed_plan_digest,
                    reviewed_application_id=application_id,
                    installation_policy=installation_policy,
                    scope=FleetProfileScope(node_ids=list(frozen_nodes)),
                    assignments=list(frozen_assignments),
                )
            execution_nodes = {
                node_id for step in preview.steps for node_id in step.node_ids
            }
            if not execution_nodes <= set(frozen_nodes):
                raise FleetProfileStalePlanConflict(
                    "Profile switch scope changed during admission"
                )
            whole_fleet_intent = selected_application or retry_of_application_id is None
            prior_profile_work = (
                session.scalar(
                    select(FleetProfileApplication.id)
                    .where(
                        FleetProfileApplication.id != application_id,
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                    )
                    .limit(1)
                )
                if whole_fleet_intent and self._switch_adapter is not None
                else None
            )
            fenced_nodes = (
                set(frozen_nodes)
                if whole_fleet_intent
                and self._switch_adapter is not None
                and (execution_nodes or prior_profile_work is not None)
                else execution_nodes
            )
            attempt = 1
            recovery_ordinal: int | None = None
            accepted_images: dict[str, RuntimeImageIdentity] = {}
            reviewed_application: FleetProfileApplication | None = None
            if retry_of_application_id is not None:
                parent = retry_parent
                if parent is None:
                    raise MissingRecord(
                        retry_of_application_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                prior = retry_parent_progress or _persisted_profile_progress(parent)
                if parent.state not in job_states.words(
                    LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
                ):
                    raise FleetProfileInvalid(
                        "Only failed or waiting applications can be retried",
                        reason=InvalidRequestReason.NOT_READY,
                    )
                profile_filter = (
                    FleetProfileApplication.profile_id.is_(None)
                    if parent.profile_id is None
                    else FleetProfileApplication.profile_id == parent.profile_id
                )
                applications = session.scalars(
                    select(FleetProfileApplication).where(
                        profile_filter,
                        FleetProfileApplication.id != parent.id,
                    )
                )
                for other in applications:
                    try:
                        other_progress = _canonical_progress(other.progress)
                    except (TypeError, ValueError):
                        # Unreadable history never blocks a new application.
                        retire_as_unknown(
                            "profile-progress",
                            str(other.id),
                            BookkeepingReason.PERSISTED_STATE_DAMAGED,
                            "skipped while checking for a superseding application",
                        )
                        continue
                    if (
                        other_progress.retry_of_application_id == parent.id
                        or other.state in {"queued", "running"}
                        or (other_progress.workload_intent_ordinal or 0)
                        > (prior.workload_intent_ordinal or 0)
                    ):
                        raise FleetProfileInvalid(
                            "Application has been superseded by another application",
                            reason=InvalidRequestReason.SUPERSEDED,
                        )
                if prior.intended_profile is None:
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                        "the receipt carries no accepted intent to recover",
                    )
                if self._superseding_intent(session, parent, prior):
                    raise FleetProfileInvalid(
                        "Application has been superseded by another workload intent",
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                parent_intent = self._intended_profile(parent, session=session)
                if isinstance(parent_intent, Residue):
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                        parent_intent.note,
                    )
                intended = parent_intent
                reviewed_application = session.get(
                    FleetProfileApplication, intended.reviewed_application_id
                )
                if reviewed_application is None:
                    raise FleetProfileUnavailable(
                        "Persisted application review source is unavailable",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                reviewed_plan = _persisted_profile_plan(reviewed_application)
                if isinstance(reviewed_plan, Residue):
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_REVIEW_UNAVAILABLE,
                        "the reviewed plan of the receipt cannot be read",
                    )
                _validate_remaining_effects(reviewed_plan.effects, preview.effects)
                attempt = prior.attempt + 1
                _require_recovery_preparations(reviewed_plan, preview)
                accepted_images = {
                    item.assignment_id: item.runtime_image
                    for item in reviewed_plan.preparation_decisions
                }
                if automatic_cache_recovery:
                    # A receipt that recorded no workload intent takes a fresh one
                    # from the node fence below, like any other admission.
                    recovery_ordinal = prior.workload_intent_ordinal
            control = self._control_effects(
                session,
                frozen_assignments,
                set(frozen_nodes),
                installation_policy,
                expected_images=accepted_images,
                excluded_application_id=application_id,
            )
            if (
                control.effects != preview.effects
                or control.changed_nodes != execution_nodes
                or any(reason.severity == "error" for reason in control.reasons)
                or any(
                    control.states[item.assignment_id].current_state
                    != item.current_state
                    for item in preview.assignments
                )
            ):
                raise FleetProfileStalePlanConflict(
                    "Profile workload effects changed during admission; review again"
                )
            affected_nodes = [
                node for node in scope_nodes if node.node_id in fenced_nodes
            ]
            intent_order = (
                _application_order_key(session, existing)
                if existing is not None
                else _application_order_key(session, reviewed_application)
                if reviewed_application is not None
                else (_aware(created_at), application_id)
            )
            if not selected_application:
                current_selection = session.get(
                    FleetProfileSelection, 1, with_for_update={"nowait": True}
                )
                current_selected_application = (
                    session.get(
                        FleetProfileApplication, current_selection.application_id
                    )
                    if current_selection is not None
                    else None
                )
                current_selected_order = None
                if current_selected_application is not None:
                    try:
                        current_selected_order = _application_order_key(
                            session, current_selected_application
                        )
                    except FleetProfileConflict:
                        # The selection FK and receipt columns remain
                        # sufficient to order this accepted root against a
                        # fresh request. Corrupt retry/progress history must
                        # not veto unrelated newly reviewed work.
                        current_selected_order = (
                            _aware(current_selected_application.created_at),
                            current_selected_application.id,
                        )
                if (
                    current_selection is not None
                    and current_selected_application is not None
                    and current_selected_application.selection_generation
                    == current_selection.generation
                    and current_selected_order is not None
                    and current_selected_order > intent_order
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Profile load was superseded by a newer accepted profile"
                    )
            if _newer_profile_intent_overlaps(session, intent_order, fenced_nodes):
                raise _FleetProfileSupersededIntentConflict(
                    "Profile intent was superseded by a later accepted overlapping request"
                )
            if self._switch_adapter is not None:
                self._switch_adapter.validate_resources_in_session(
                    session, frozen_assignments, preview
                )
            try:
                lock_profile_build_dependencies(session, preview)
            except BuildConsumerError as error:
                if error.retryable:
                    # The build is being cancelled or is busy: admission waits
                    # and is retried by its owner (it is parked, not refused).
                    raise FleetProfileAdmissionEffectBusy(
                        f"{error.code}: {error}"
                    ) from error
                # The reviewed build record is gone or unreadable: there is no
                # running build to protect, and the exact image identity is held
                # by the plan itself and verified again when its child starts.
                retire_as_unknown(
                    "profile-build-dependency",
                    application_id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    f"{error.code}: {error}",
                )
            workload_intent_ordinal = (
                recovery_ordinal
                if recovery_ordinal is not None
                else pending_ordinal
                if pending_ordinal is not None
                else max(node.workload_intent_ordinal for node in affected_nodes) + 1
                if affected_nodes
                else None
            )
            if pending_ordinal is not None and any(
                node.workload_intent_ordinal != pending_ordinal
                for node in affected_nodes
            ):
                raise _FleetProfileSupersededIntentConflict(
                    "Profile workload intent was superseded before admission resumed"
                )
            if workload_intent_ordinal is not None:
                for node in affected_nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                # (With no executor bound yet the older workloads' cancellation is
                # requested by the load's own worker when its executor is bound:
                # it requests it again for the same node fence and intent before
                # it completes.)
                if pending_ordinal is None and self._switch_adapter is not None:
                    self._switch_adapter.request_superseded_workload_cancellation_in_session(
                        session,
                        tuple(sorted(fenced_nodes)),
                        workload_intent_ordinal,
                        now,
                    )
                for prior_application in session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        )
                    )
                    .order_by(FleetProfileApplication.id)
                    .with_for_update(nowait=True)
                ):
                    prior_plan = _persisted_profile_plan(prior_application)
                    if isinstance(prior_plan, Residue):
                        # Its plan cannot be read, so what it owns is unknown: it is
                        # retired by the newer authority (its agent effects were
                        # independently fenced above, and keep their own receipts).
                        self._lifecycle.cancelled(
                            prior_application,
                            "Profile order retired by a later scoped intent; its "
                            "stored plan could not be read, so its effect is unknown",
                            now,
                            effect=_LifecycleEffect.UNKNOWN,
                            session=session,
                        )
                        continue
                    try:
                        prior_scope = {
                            node_id
                            for step in prior_plan.steps
                            for node_id in step.node_ids
                        }
                        if not prior_scope & fenced_nodes:
                            continue
                        prior_progress = _persisted_profile_progress(prior_application)
                        prior_order = _application_order_key(session, prior_application)
                    except FleetProfileConflict as error:
                        # Quarantine the invalid order, retaining its evidence.
                        # Its agent effects were independently fenced above;
                        # malformed history cannot roll back the new authority.
                        self._lifecycle.fail(
                            prior_application, str(error), now, session=session
                        )
                        continue
                    prior_ordinal = prior_progress.workload_intent_ordinal
                    if prior_progress.admission_pending:
                        # A parked receipt has not passed admission and owns no
                        # selected profile or workload order. Keep a newer one
                        # parked; an older one is superseded by this acceptance.
                        if prior_order > intent_order:
                            continue
                        self._lifecycle.supersede(
                            prior_application,
                            "Profile order was replaced before admission by a later "
                            "scoped intent",
                            now,
                            code=SupersedeCode.SUPERSEDED_BY_INTENT,
                            by=application_id,
                            session=session,
                        )
                        continue
                    if (
                        prior_order > intent_order
                        and prior_progress.intended_profile is not None
                    ):
                        # New pending receipts do not acquire the node fence.
                        # A later accepted receipt can therefore appear after
                        # the earlier overlap scan; preserve it instead of
                        # cancelling it as though it were an older pending row.
                        raise _FleetProfileSupersededIntentConflict(
                            "Profile intent was superseded by a later accepted overlapping request"
                        )
                    if (
                        prior_ordinal is None
                        or prior_ordinal >= workload_intent_ordinal
                    ):
                        continue
                    self._lifecycle.supersede(
                        prior_application,
                        "Profile order was replaced by a later scoped intent; "
                        "issued effects retain their own cancellation receipts",
                        now,
                        code=SupersedeCode.SUPERSEDED_BY_INTENT,
                        by=application_id,
                        effect=_LifecycleEffect.UNKNOWN,
                        session=session,
                    )
            progress = FleetProfileApplicationProgress(
                operation_kind=operation_kind,
                attempt=attempt,
                retry_of_application_id=retry_of_application_id,
                intended_profile=intended,
                workload_intent_ordinal=workload_intent_ordinal,
                completed_steps=0,
                total_steps=len(preview.steps),
            ).model_dump(mode="json")
            row = existing
            if row is None:
                row = FleetProfileAdapter.new_application(
                    id=application_id,
                    request_key=request_key,
                    profile_id=preview.profile_id,
                    profile_digest=preview.profile_digest,
                    plan_digest=preview.plan_digest,
                    state="succeeded" if not preview.steps else "queued",
                    plan=preview.model_dump(mode="json"),
                    current_step=0,
                    current_operation_id=None,
                    selection_generation=(
                        selected_generation if selected_application else None
                    ),
                    progress=progress,
                    result=(
                        FleetProfileApplicationResult(
                            changed=False, completed_steps=0
                        ).model_dump(mode="json")
                        if not preview.steps
                        else None
                    ),
                    actor=actor,
                    created_at=created_at,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.profile_id = preview.profile_id
                row.profile_digest = preview.profile_digest
                row.plan_digest = preview.plan_digest
                self._lifecycle.reset(
                    row, now, steps=bool(preview.steps), session=session
                )
                row.plan = preview.model_dump(mode="json")
                row.current_step = 0
                row.current_operation_id = None
                row.progress = progress
                row.result = (
                    FleetProfileApplicationResult(
                        changed=False, completed_steps=0
                    ).model_dump(mode="json")
                    if not preview.steps
                    else None
                )
                row.status_reason = None
                row.updated_at = now
            session.flush()
            if retry_of_application_id is not None and existing is None:
                if retry_parent is not None and automatic_cache_recovery:
                    # An ended parent absorbs the event; the reason is still the
                    # record of why no attempt is scheduled for it.
                    # The earlier failure stays in the reason: it is what the
                    # operator needs, and the successor carries the continuation.
                    earlier = (retry_parent.status_reason or "").strip()
                    superseded_by = (
                        f"Automatically reconciled by profile retry {row.id}"
                        + (f"; earlier failure: {earlier}" if earlier else "")
                    )[:512]
                    self._lifecycle.supersede(
                        retry_parent,
                        superseded_by,
                        now,
                        code=SupersedeCode.SUPERSEDED_BY_RETRY,
                        by=row.id,
                        session=session,
                    )
                    retry_parent.status_reason = superseded_by
                    # Superseded by its retry: no attempt is scheduled for it.
                    retry_parent.progress = _progress_with_blockers(
                        _canonical_progress(retry_parent.progress),
                        [],
                        retry_due_at=None,
                    )
                    retry_parent.updated_at = now
                if (
                    selected_generation is None
                    or selected_application_id is None
                    or selected_roster_digest is None
                ):
                    raise FleetProfileSelectionLost(
                        "Selected profile retry lost its current selection"
                    )
                row.selection_generation = selected_generation
                _replace_selected_profile_application(
                    session,
                    expected_generation=selected_generation,
                    expected_application_id=selected_application_id,
                    expected_roster_digest=selected_roster_digest,
                    application_id=row.id,
                    now=now,
                )
                session.flush()
            reserve_profile_disk(
                session,
                row,
                preview,
                {
                    item.id
                    for item in frozen_assignments
                    if not control.states[item.id].installation_ready
                },
                now=now,
            )
            runtime_assignments = {
                item.id
                for item in frozen_assignments
                if item.desired_state == DesiredAssignmentState.RUNNING
                and control.states[item.id].current_state
                != ObservedAssignmentState.RUNNING
            }
            reserve_profile_ports(
                session,
                row,
                preview,
                runtime_assignments,
                now=now,
            )
            reserve_profile_memory(
                session,
                row,
                preview,
                runtime_assignments,
                now=now,
            )
            current_scope = tuple(
                session.scalars(
                    select(AgentNode.node_id)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            if current_scope != frozen_nodes:
                raise FleetProfileStalePlanConflict(
                    "Profile fleet scope changed during admission; review again"
                )
            if pending_roster_reconciliation:
                if (
                    selected_generation is None
                    or selected_application_id is None
                    or selected_roster_digest is None
                ):
                    raise FleetProfileStalePlanConflict(
                        "Selected profile changed before roster admission"
                    )
                row.selection_generation = _replace_selected_profile_roster(
                    session,
                    expected_generation=selected_generation,
                    expected_application_id=selected_application_id,
                    expected_roster_digest=selected_roster_digest,
                    profile_id=preview.profile_id,
                    profile_revision=(
                        preview.profile_revision
                        if preview.profile_revision is not None
                        else profile.revision
                    ),
                    application_id=row.id,
                    node_ids=frozen_nodes,
                    now=now,
                )
            elif (
                existing is not None
                and existing_progress is not None
                and existing_progress.admission_pending
                and existing.selection_generation is None
                and operation_kind == "fleet-profile.apply"
            ):
                if preview.profile_revision is None:
                    raise FleetProfileUnavailable(
                        "Selected profile revision is unavailable",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                row.selection_generation = _set_selected_profile(
                    session,
                    profile_id=preview.profile_id,
                    profile_revision=preview.profile_revision,
                    application_id=row.id,
                    node_ids=frozen_nodes,
                    now=now,
                )
            session.flush()
            return self._application_view(row)

    def _resume_live_child(
        self, application_id: str
    ) -> FleetProfileApplicationView | None:
        """Hand a failed order whose child is still live back to advancement.

        A transient error while advancing a live child marks the order failed,
        but only the running path ever advances that child again.  Refusing a
        retry because the child "is still active" therefore parked the current
        profile intent forever (live: a profile load stopped the old run, then
        never started the new one).  The child is the same authorized work, so
        continuing it is the retry; the bounded retry schedule rate-limits it.
        """

        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if (
                row is None
                or row.state
                not in job_states.words(
                    LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
                )
                or not row.current_operation_id
                or self._switch_adapter is None
            ):
                return None
            try:
                child = self._switch_adapter.get(
                    row.current_operation_id, session=session
                )
            except (KeyError, RuntimeError, ValueError):
                return None
            if child.state not in _CHILD_PENDING_STATES:
                return None
            self._lifecycle.reopen(
                row,
                now,
                reason="Resumed advancing the live child operation",
                session=session,
            )
            # Failing released its unassigned claims; the resumed child needs them.
            restore_released_profile_claims(session, row)
            session.flush()
            return self._application_view(row)

    def retry_eligible(self, application_id: str) -> bool:
        """Whether this receipt is still the current recoverable profile intent."""
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            return row is not None and self._retry_eligible(session, row)

    def _retry_eligible(self, session: Session, row: FleetProfileApplication) -> bool:
        if row.state not in job_states.words(
            LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
        ):
            return False
        try:
            progress = _canonical_progress(row.progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            # A damaged receipt cannot prove that it still carries the current
            # recoverable intent, so it simply does not advertise retry.
            return False
        if row.selection_generation is not None:
            if not self._application_is_current_selection(session, row, progress):
                return False
        else:
            profile = session.get(FleetProfile, row.profile_id)
            try:
                current_profile_digest = (
                    _digest(_profile_document(profile)) if profile is not None else None
                )
            except (
                FleetProfileConflict,
                KeyError,
                TypeError,
                ValidationError,
                ValueError,
            ):
                return False
        if progress.intended_profile is None or (
            row.selection_generation is None
            and current_profile_digest != row.profile_digest
        ):
            return False
        try:
            superseded = self._superseding_intent(session, row, progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            return False
        if superseded:
            return False
        try:
            plan = _persisted_profile_plan(row)
            if isinstance(plan, Residue):
                return False
            effect_nodes = {node_id for step in plan.steps for node_id in step.node_ids}
            if _newer_profile_intent_overlaps(
                session, _application_order_key(session, row), effect_nodes
            ):
                return False
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            return False
        # The scoped accepted-order scan above catches later overlapping work,
        # and _superseding_intent checks the durable node ordinals. This final
        # profile-level pass only checks for active siblings or explicit retry
        # children; malformed unrelated history must not deny a valid retry.
        others = session.scalars(
            select(FleetProfileApplication).where(
                FleetProfileApplication.profile_id == row.profile_id,
                FleetProfileApplication.id != row.id,
            )
        )
        return not any(
            other.state in {"queued", "running"}
            or _stored_retry_lineage(other.progress) == row.id
            for other in others
        )

    def bind_preparation_starter(self, starter: PreparationStarter) -> None:
        """Attach the Controller's preparation authority after startup wiring."""

        self._preparation_starter = starter

    def bind_storage_relief(self, relief: StorageReliefProvider) -> None:
        """Attach the authority that frees disk for a load waiting on it."""

        self._storage_relief = relief

    def bind_preparation_canceller(self, canceller: PreparationCanceller) -> None:
        """Attach the authority that cancels a preparation a load asked for."""

        self._preparation_canceller = canceller

    def _cancel_owned_preparations(
        self, application_id: str, plan: FleetProfilePreview, *, actor: str
    ) -> None:
        """Cancel what a cancelled waiting load asked the Controller to prepare.

        Best effort and self-healing: the application is already cancelled, so
        a preparation left behind is only useful work the next load reuses.
        Revisions another live application still waits on are kept.
        """

        canceller = self._preparation_canceller
        if canceller is None:
            return
        revisions = {
            assignment.recipe_revision_id
            for assignment in _assignments_needing_preparation(
                plan.assignments,
                {item.assignment_id for item in plan.preparations},
                plan.reasons,
                plan.assessments,
            )
        }
        cancelled: list[str] = []
        for revision_id in sorted(revisions):
            with self._sessions() as session:
                others = tuple(
                    session.scalars(
                        select(FleetProfileApplication).where(
                            FleetProfileApplication.id != application_id,
                            FleetProfileApplication.state.in_(
                                job_states.words(
                                    LifecycleState.QUEUED,
                                    LifecycleState.RUNNING,
                                    LifecycleState.NEEDS_OPERATOR,
                                )
                            ),
                        )
                    )
                )
                shared = False
                for other in others:
                    other_plan = _persisted_profile_plan(other)
                    if isinstance(other_plan, Residue):
                        continue
                    if any(
                        item.recipe_revision_id == revision_id
                        for item in other_plan.resolved_assignments
                    ):
                        shared = True
                        break
            if shared:
                continue
            try:
                cancelled.extend(
                    canceller(
                        revision_id,
                        actor=actor,
                        reason=f"Profile application {application_id} was cancelled",
                    )
                )
            except Exception:  # the cancel already succeeded
                _LOGGER.warning(
                    "could not cancel preparation of %s for cancelled application %s",
                    revision_id,
                    application_id,
                    exc_info=True,
                )
        if cancelled:
            with self._sessions.begin() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is not None and row.state == "cancelled":
                    row.status_reason = (
                        (row.status_reason or "")
                        + f" Cancelled pending preparation: {', '.join(cancelled)}."
                    )[:512]

    def _request_preparations(
        self, preview: FleetProfilePreview, *, actor: str
    ) -> list[OperationBlocker]:
        """Enqueue the model and image preparation a blocked load is missing.

        A load asks for what it needs: an assignment that must place assets the
        Controller has not prepared gets its preparation started here, and the
        returned reasons say how far along it is. Nothing the fleet or recipe
        cannot resolve by itself starts a preparation.
        """

        blockers = self._request_storage(preview)
        starter = self._preparation_starter
        if starter is None:
            return blockers
        for assignment in _assignments_needing_preparation(
            preview.assignments,
            {item.assignment_id for item in preview.preparations},
            preview.reasons,
            preview.assessments,
        ):
            try:
                blockers.extend(starter(assignment.recipe_revision_id, actor=actor))
            except Exception as error:  # noqa: BLE001 - a load never fails on this
                blockers.append(
                    make_blocker(
                        ProfileReasonCode.PREPARATION_NOT_STARTED,
                        f"Preparing {assignment.recipe_title} could not be "
                        f"started yet: {error}",
                        severity="warning",
                    )
                )
        return blockers

    def _request_storage(self, preview: FleetProfilePreview) -> list[OperationBlocker]:
        """Ask for the disk a load was refused for, and say how that is going.

        A load that does not fit a Spark's free disk waits (it is not refused for
        good): the Controller removes the least recently used installations
        nothing uses until it fits, and the load retries by itself. The reason
        shown names the bytes needed and the bytes that can be freed.
        """

        if self._storage_relief is None:
            return []
        blockers: list[OperationBlocker] = []
        for item in preview.assessments:
            for node_id, needed in _disk_shortfalls(item.assessment):
                found = self._ask_storage_relief(node_id, needed, preview.profile_id)
                if found is not None:
                    blockers.append(_relief_blocker(node_id, found))
        return blockers

    def _ask_storage_relief(
        self, node_id: str, required_free_bytes: int, profile_id: str
    ) -> StorageRelief | None:
        """Register the demand on one Spark and read what eviction can do."""

        relief = self._storage_relief
        if relief is None:
            return None
        try:
            return relief(
                node_id,
                required_free_bytes,
                source="profile-load",
                subject=profile_id,
                reason=RunSwitchCode.INSUFFICIENT_DISK,
            )
        except Exception:  # a load never fails on this
            _LOGGER.warning("storage relief failed", exc_info=True)
            return None

    def _park_for_retry(
        self,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        blockers: Sequence[OperationBlocker],
        *,
        because: BaseException | None = None,
    ) -> None:
        """Record why an application waits and when it will be checked again.

        ``because`` is the error that sends the application here: only an error
        declared retryable-by-waiting may park, anything else is a defect in the
        caller (waiting would never resolve it).

        The application is not failed: it keeps its accepted intent and the
        Controller retries it when conditions change. Its blockers replace the
        previous list, and one log line names a change of reason (not every retry).
        """

        assert because is None or retry_disposition_of(because) == RETRY_WAIT, (
            f"{type(because).__name__} is not retryable by waiting and must "
            "not park an application"
        )
        now = _aware(self._clock())
        due = FleetProfileAdapter.next_retry(row.id, progress.attempt, now)
        blockers = bound_blockers(blockers)
        row.progress = _progress_with_blockers(
            progress,
            blockers,
            attempt=progress.attempt + 1,
            retry_due_at=due.isoformat(),
        )
        lead = (
            f"{blockers[0].code}: {blockers[0].detail}"
            if blockers
            else "current Fleet conditions"
        )
        row.status_reason = (
            f"Waiting to retry ({lead}); next attempt at {due.isoformat()}"
        )[:512]
        row.updated_at = now
        previous = {(item.code, tuple(item.node_ids)) for item in progress.blockers}
        if previous != {(item.code, tuple(item.node_ids)) for item in blockers}:
            _LOGGER.log(
                logging.WARNING
                if any(item.severity == "error" for item in blockers)
                else logging.INFO,
                "profile application %s is waiting: %s; next attempt %s",
                row.id,
                "; ".join(f"{item.code}: {item.detail}" for item in blockers[:4])
                or "current Fleet conditions",
                due.isoformat(),
            )

    def _decline_retry(
        self,
        parent: FleetProfileApplication,
        code: str,
        detail: str,
        blockers: Sequence[OperationBlocker] = (),
    ) -> FleetProfileApplicationView:
        """A recovery that cannot be started now is an unknown, never a refusal.

        The receipt keeps its accepted intent.  While it is still the current
        recoverable intent it is parked: its reason and next attempt are visible
        and the Controller looks again with bounded backoff.  Once it is not (a
        newer intent or another fleet owns the scope) it is left as it ended, with
        the reason noted; the selected profile's own reconciliation, or a new load,
        issues the replacement.  The caller gets the receipt either way.
        """

        retire_as_unknown(
            "profile-retry", str(parent.id), BookkeepingReason.EVIDENCE_MISMATCH, detail
        )
        session = object_session(parent)
        if session is not None and self._retry_eligible(session, parent):
            self._park_for_retry(
                parent,
                _persisted_profile_progress(parent),
                list(blockers) or [make_blocker(code, detail)],
            )
        else:
            prior = (parent.status_reason or "").split(" | retry not started")[0]
            parent.status_reason = f"{prior} | retry not started: {detail}"[:512]
            parent.updated_at = _aware(self._clock())
        return self._application_view(parent)

    def _decline_retry_application(
        self,
        application_id: str,
        code: str,
        detail: str,
        blockers: Sequence[OperationBlocker] = (),
    ) -> FleetProfileApplicationView:
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            return self._decline_retry(row, code, detail, blockers)

    def retry(
        self,
        application_id: str,
        *,
        request_key: str,
        actor: str,
        automatic_cache_recovery: bool = False,
    ) -> FleetProfileApplicationView:
        """Persist a new reconciliation attempt, retaining the original receipt.

        What cannot be reconciled now (a child the executor cannot show, a scope or
        assignment set that changed, a plan that is not admissible yet) leaves the
        receipt parked or noted and returns it; only a request for something that
        cannot be retried at all is refused.
        """
        decline: tuple[str, str] | None = None
        with self._sessions() as session:
            self._authorize(session, actor)
            replay = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if replay is not None:
                progress = _persisted_profile_progress(replay)
                if (
                    progress.retry_of_application_id != application_id
                    or replay.actor != actor
                ):
                    raise FleetProfileInvalid(
                        "Retry request key was reused for another application",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._matching_application(
                    replay,
                    session=session,
                    profile_id=replay.profile_id,
                    actor=actor,
                    reviewed_digest=None,
                    retry_of_application_id=application_id,
                )
            parent = session.get(FleetProfileApplication, application_id)
            if parent is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(parent)
            if parent.state not in job_states.words(
                LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
            ):
                raise FleetProfileInvalid(
                    "Only failed or waiting applications can be retried",
                    reason=InvalidRequestReason.NOT_READY,
                )
            operation_kind = progress.operation_kind or "fleet-profile.apply"
            if operation_kind != "fleet-profile.apply":
                raise FleetProfileInvalid(
                    "Only current profile loads can be recovered",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            if progress.intended_profile is None:
                decline = (
                    ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                    "the receipt carries no accepted intent to recover",
                )
            adapter = self._switch_adapter
            if decline is None and parent.current_operation_id is not None:
                if adapter is None:
                    decline = (
                        ProfileReasonCode.RETRY_EXECUTOR_UNAVAILABLE,
                        "the Run/Switch executor is not bound yet",
                    )
                else:
                    try:
                        child = adapter.get(
                            parent.current_operation_id, session=session
                        )
                    except (KeyError, RuntimeError, ValueError):
                        # The child's record is gone: the step is issued again under
                        # its deterministic identity, and its owner reconciles what
                        # it already did (it is adopted, not repeated).
                        child = None
                    if child is not None and child.state in _CHILD_PENDING_STATES:
                        # End this read before the resume takes its row lock.
                        parent_id = parent.id
                        session.close()
                        resumed = self._resume_live_child(parent_id)
                        if resumed is not None:
                            return resumed
            persisted_plan = (
                None
                if decline is not None
                else self._reviewed_profile_plan(parent, session=session)
            )
            if isinstance(persisted_plan, Residue):
                decline = (
                    ProfileReasonCode.RETRY_REVIEW_UNAVAILABLE,
                    persisted_plan.note,
                )
            intended = progress.intended_profile
            cache_loss_recovery = (
                adapter is not None
                and decline is None
                and adapter.recoverable_cache_loss(parent.id, session=session)
            )
        if (
            decline is not None
            or persisted_plan is None
            or isinstance(persisted_plan, Residue)
            or intended is None
        ):
            code, detail = decline or (
                ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                "the receipt carries no accepted intent to recover",
            )
            return self._decline_retry_application(application_id, code, detail)
        preview = self.preview(
            persisted_plan.profile_id,
            execution_assignments=tuple(intended.assignments),
            profile_name=persisted_plan.profile_name,
            profile_digest=intended.profile_digest,
            accepted_intent=intended,
            accepted_profile_revision=persisted_plan.profile_revision,
            accepted_profile_definition=persisted_plan.profile_definition,
            allow_pending_cache_rebuild=cache_loss_recovery,
            profile_application_id=application_id,
        )
        if not preview.allowed:
            if not _profile_preview_is_waitable(preview):
                raise FleetProfileInvalid(
                    "Current Fleet state blocks application recovery",
                    reason=InvalidRequestReason.NOT_READY,
                )
            blockers = _preview_blockers(preview) + self._request_preparations(
                preview, actor=actor
            )
            return self._decline_retry_application(
                application_id,
                ProfileReasonCode.RECOVERY_WAITING,
                "current Fleet conditions do not admit the accepted plan yet",
                blockers,
            )
        if tuple(preview.scope.node_ids) != tuple(intended.scope.node_ids):
            return self._decline_retry_application(
                application_id,
                ProfileReasonCode.RECOVERY_SCOPE_CHANGED,
                "the fleet scope changed since the application was accepted",
            )
        expected_assignments = {
            assignment.id: (
                assignment.recipe_revision_id,
                assignment.desired_state,
                tuple(node.node_id for node in assignment.nodes),
            )
            for assignment in intended.assignments
        }
        observed_assignments = {
            assignment.assignment_id: (
                assignment.recipe_revision_id,
                assignment.desired_state,
                tuple(assignment.node_ids),
            )
            for assignment in preview.assignments
        }
        if observed_assignments != expected_assignments:
            return self._decline_retry_application(
                application_id,
                ProfileReasonCode.RECOVERY_ASSIGNMENTS_CHANGED,
                "the assignments changed since the application was accepted",
            )
        _require_recovery_preparations(persisted_plan, preview)
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind=operation_kind,
            retry_of_application_id=application_id,
            automatic_cache_recovery=automatic_cache_recovery,
        )

    def operation_provider(self) -> OperationProviderProtocol:
        """Project profile applications into the global Activity provider contract."""

        # Keep this import local so the profile domain remains usable by the
        # profile routes when the optional Activity projection is unavailable.
        from .operation_api import (
            OperationListPage,
            OperationProvider,
            OperationQuery,
            _activity_keyset_filter,
        )

        def list_operations(query: OperationQuery) -> OperationListPage:
            after = query.after
            state = query.state
            node_id = query.node_id
            limit = query.limit
            with self._sessions() as session:
                base_statement = select(FleetProfileApplication).order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                if isinstance(state, str):
                    # A filter may still name a retired spelling (one release).
                    named = input_state(state)
                    base_statement = base_statement.where(
                        _profile_activity_state_expression().in_(
                            fleet_profile_states.words(named)
                            if named is not None
                            else (state,)
                        )
                    )
                if isinstance(query.request_id, str):
                    base_statement = base_statement.where(
                        FleetProfileApplication.request_key == query.request_id
                    )
                # The plan is canonical JSON. Quoted containment avoids matching
                # a node-id substring while keeping this projection portable across
                # PostgreSQL JSON and SQLite JSON test databases.
                if isinstance(node_id, str):
                    base_statement = base_statement.where(
                        cast(
                            FleetProfileApplication.plan["scope"]["node_ids"],
                            String,
                        ).like(f'%"{node_id}"%')
                    )
                total = int(
                    session.scalar(
                        select(func.count()).select_from(base_statement.subquery())
                    )
                    or 0
                )
                statement = base_statement
                boundary = _activity_keyset_filter(
                    FleetProfileApplication.created_at,
                    FleetProfileApplication.id,
                    "",
                    after,
                )
                if boundary is not None:
                    statement = statement.where(boundary)
                rows = tuple(session.scalars(statement.limit(limit)))
                return OperationListPage(
                    items=[self._activity_item(session, row) for row in rows],
                    next_cursor=None,
                    total=total,
                )

        def get_operation(operation_id: str) -> Mapping[str, object]:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, operation_id)
                if row is None:
                    raise MissingRecord(
                        operation_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                return self._activity_item(session, row)

        return OperationProvider(
            family="fleet-profile",
            list_operations=list_operations,
            get_operation=get_operation,
        )

    def _activity_item(
        self, session: Session, row: FleetProfileApplication
    ) -> dict[str, object]:
        """One Activity row, with retry facts read once in the row's session."""

        try:
            progress = _canonical_progress(row.progress)
            retrying = row.state == "failed" and self._recovery_wanted(
                session, row, progress
            )
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            retrying = False
        return self._operation_item(
            row,
            retry_available=self._retry_eligible(session, row),
            retrying=retrying,
        )

    @staticmethod
    def _operation_scope(plan: FleetProfilePreview) -> tuple[str, ...]:
        return tuple(plan.scope.node_ids)

    @classmethod
    def _operation_phase(
        cls,
        row: FleetProfileApplication,
        plan: FleetProfilePreview,
        progress: FleetProfileApplicationProgress,
    ) -> str:
        if row.state == "succeeded":
            return "final_verify"
        if 0 <= row.current_step < len(plan.steps):
            kind = plan.steps[row.current_step].kind
            if kind in {"create-placement", "build", "install"}:
                return "prepare"
            if kind == "distribute-image":
                return "transfer"
            if kind == "stop":
                return "stop"
            if kind == "uninstall":
                return "cleanup"
            if kind == "start":
                return "start"
            if kind == "switch":
                if progress.child_progress is not None:
                    return progress.child_progress.phase
                return "prepare"
        return "final_verify"

    @classmethod
    def _operation_item(
        cls,
        row: FleetProfileApplication,
        *,
        retry_available: bool = False,
        retrying: bool = False,
    ) -> dict[str, object]:
        """Project profile progress and its operator-visible failure into Activity.

        A single damaged historical row must not fail the whole page and must
        not be hidden as an empty success: its unreadable document becomes an
        explicit failure on that record while every readable record stays
        usable.
        """

        try:
            typed_progress = _canonical_progress(row.progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            warn_unreadable_once("profile application", row.id)
            return cls._unreadable_operation_item(row)
        plan = _persisted_profile_plan(row)
        result = _persisted_profile_result(row)
        if isinstance(plan, Residue):
            warn_unreadable_once("profile application", row.id)
            return cls._unreadable_operation_item(row)
        cancellation = _application_cancellation_view(row, plan, typed_progress)
        state = _profile_activity_state(row.state, typed_progress.cancellation)
        if retrying and state == "failed":
            # The Controller will retry this by itself: it is waiting, not failed.
            state = "queued"
        next_attempt = None
        if state == "queued":
            next_attempt = (
                typed_progress.retry_due_at
                if retrying
                else typed_progress.admission_retry_at
                if typed_progress.admission_pending
                else None
            )
        failure = None
        if state in job_states.words(
            LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
        ):
            # A failed row that recorded no reason still shows what is known of it.
            reason = (
                row.status_reason
                if row.status_reason and row.status_reason.strip()
                else f"The profile application ended {state} without a recorded reason"
            )
            failure = OperationFailureEvidence(
                error_code=OperationFailureCode.FLEET_PROFILE_APPLICATION_FAILED,
                summary=(
                    "Profile application needs attention"
                    if state in fleet_profile_states.NEEDS_OPERATOR
                    else "Profile application failed"
                ),
                detail=redact_text(reason),
                retryable=retry_available,
                uncertain=state in fleet_profile_states.NEEDS_OPERATOR,
            ).model_dump(mode="json")
        return {
            "id": row.id,
            "parent_id": typed_progress.retry_of_application_id,
            "node_ids": list(cls._operation_scope(plan)),
            "kind": typed_progress.operation_kind or "fleet-profile.apply",
            "state": state,
            "attempt": typed_progress.attempt,
            "progress": {"phase": cls._operation_phase(row, plan, typed_progress)},
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "supported_actions": ["retry"] if retry_available else [],
            "owner": {
                "kind": "fleet-profile-application",
                "id": row.id,
                "request_id": row.request_key,
            },
            "failure": failure,
            "superseded_by": (
                typed_progress.superseded_by if state == "superseded" else None
            ),
            "reason_code": (
                typed_progress.supersede_code if state == "superseded" else None
            ),
            "result": result.model_dump(mode="json") if result is not None else None,
            "cancellation": (
                cancellation.model_dump(mode="json")
                if cancellation is not None
                else None
            ),
            "status_reason": (
                redact_text(row.status_reason)
                if (cancellation is not None or state in {"queued", "superseded"})
                and row.status_reason is not None
                else None
            ),
            "blockers": (
                [item.model_dump(mode="json") for item in typed_progress.blockers]
                if state
                in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.FAILED,
                    LifecycleState.NEEDS_OPERATOR,
                )
                else []
            ),
            "next_attempt_at": next_attempt.isoformat()
            if next_attempt is not None
            else None,
        }

    @staticmethod
    def _unreadable_operation_item(row: FleetProfileApplication) -> dict[str, object]:
        """Project a damaged application instead of letting it break Activity.

        Durable columns stay authoritative for identity and declared scope;
        only the unreadable document is replaced by an explicit failure, and
        no retry is offered because intent cannot be proven.
        """

        return {
            "id": row.id,
            "parent_id": None,
            "node_ids": list(_persisted_profile_scope(row) or ()),
            "kind": "fleet-profile.apply",
            "state": row.state,
            "attempt": 0,
            "progress": None,
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "supported_actions": [],
            "owner": {
                "kind": "fleet-profile-application",
                "id": row.id,
                "request_id": row.request_key,
            },
            "result": None,
            "status_reason": "Stored profile application record is unreadable.",
        }

    def application(self, application_id: str) -> FleetProfileApplicationView:
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            return self._application_view(row)

    def application_cancellation_by_request(
        self,
        application_id: str,
        request_key: str,
        *,
        actor: str,
    ) -> FleetProfileApplicationView:
        """Resolve one accepted cancellation for its exact actor and owner."""

        with self._sessions() as session:
            self._authorize(session, actor, mutation=False)
            current_role = session.scalar(
                select(User.role).where(User.subject == actor)
            )
            if (
                current_role
                not in MUTATION_ROLES[
                    ("POST", "/api/profile/applications/{application_id}/cancel")
                ]
            ):
                raise FleetProfilePermissionDenied(
                    "Current profile cancellation authority is unavailable"
                )
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            intent = _persisted_profile_progress(row).cancellation
            if (
                intent is None
                or intent.cause != "operator"
                or intent.request_key != request_key
                or intent.actor != actor
            ):
                raise MissingRecord(request_key, reason=InvalidRequestReason.NOT_FOUND)
            return self._application_view(row)

    def application_by_request_key(
        self,
        request_key: str,
        *,
        actor: str,
        number: int | None = None,
    ) -> FleetProfileApplicationView:
        """Resolve one accepted submission after its response was lost."""

        with self._sessions() as session:
            self._authorize(session, actor, mutation=False)
            row = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if row is None or row.actor != actor:
                raise MissingRecord(request_key, reason=InvalidRequestReason.NOT_FOUND)
            if (
                number is not None
                and session.scalar(
                    select(FleetProfile.number).where(FleetProfile.id == row.profile_id)
                )
                != number
            ):
                raise MissingRecord(request_key, reason=InvalidRequestReason.NOT_FOUND)
            return self._application_view(row)

    def cancel(
        self,
        application_id: str,
        *,
        profile_number: int,
        request_key: str,
        actor: str,
    ) -> FleetProfileApplicationView:
        """Persist one exact cancellation and reconcile only issued effects."""

        try:
            parsed_key = uuid.UUID(request_key)
        except (TypeError, ValueError, AttributeError) as error:
            raise FleetProfileInvalid(
                "Profile cancellation request key is invalid",
                reason=InvalidRequestReason.MALFORMED,
            ) from error
        if str(parsed_key) != request_key:
            raise FleetProfileInvalid(
                "Profile cancellation request key is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )

        with self._sessions() as snapshot_session:
            self._authorize(snapshot_session, actor)
            snapshot = snapshot_session.get(FleetProfileApplication, application_id)
            if snapshot is None:
                raise FleetProfileInvalid(
                    application_id, reason=InvalidRequestReason.MALFORMED
                )
            snapshot_number = snapshot_session.scalar(
                select(FleetProfile.number).where(
                    FleetProfile.id == snapshot.profile_id
                )
            )
            if snapshot_number != profile_number:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            snapshot_scope = _application_effect_scope(snapshot)

        now = _aware(self._clock())
        cancellation: FleetProfileApplicationCancellationIntent | None = None
        waiting_only = False
        with self._admission_session(actor, node_ids=snapshot_scope) as session:
            row = session.get(
                FleetProfileApplication,
                application_id,
                with_for_update={"nowait": True},
            )
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            if (
                session.scalar(
                    select(FleetProfile.number).where(FleetProfile.id == row.profile_id)
                )
                != profile_number
            ):
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )

            progress = _persisted_profile_progress(row)
            plan = _persisted_profile_plan(row)
            scope = _application_effect_scope(row)
            if scope != snapshot_scope:
                raise FleetProfileInvalid(
                    "Profile application scope changed during cancellation",
                    reason=InvalidRequestReason.CONFLICT,
                )
            previous = progress.cancellation
            if previous is not None:
                if (
                    previous.cause != "operator"
                    or previous.request_key != request_key
                    or previous.actor != actor
                ):
                    raise FleetProfileInvalid(
                        "Profile application already has a different cancellation request",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                cancellation = previous
            else:
                # A failed application the Controller will retry by itself is
                # shown as queued, so it must be cancellable like any queued
                # one: cancelling stops that retry, and a child it still owns is
                # reconciled by the same fence and cancellation as for a live
                # application.
                retrying_failure = row.state == "failed" and self._recovery_wanted(
                    session, row, progress
                )
                if (
                    row.state
                    not in job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                    and not retrying_failure
                ):
                    raise FleetProfileInvalid(
                        "Profile application is not cancellable",
                        reason=InvalidRequestReason.NOT_READY,
                    )
                ordinal = progress.workload_intent_ordinal
                # A workload fence precedes every workload effect, so an issued
                # child without one is evidence this cancel cannot fence: it is not
                # refused for it (a cancel always completes).  It is driven like any
                # other: the child is asked to stop and observed, and the cancel ends
                # within its budget with that child's effect recorded.
                has_child = (
                    progress.switch_adapter is not None
                    or row.current_operation_id is not None
                )
                cancel_ordinal: int | None = None
                if ordinal is not None:
                    cancel_ordinal = ordinal + 1
                    nodes = (
                        tuple(
                            session.scalars(
                                select(AgentNode)
                                .where(AgentNode.node_id.in_(scope))
                                .order_by(AgentNode.node_id)
                                .with_for_update(nowait=True)
                            )
                        )
                        if scope
                        else ()
                    )
                    # A node whose fence is not this application's is owned by a
                    # newer (or an unrecorded) intent: it is left alone, and the
                    # cancel completes for the nodes this application still owns.
                    cancellable_nodes = tuple(
                        node.node_id
                        for node in nodes
                        if node.workload_intent_ordinal == ordinal
                    )
                    for node in nodes:
                        if node.workload_intent_ordinal == ordinal:
                            node.workload_intent_ordinal = cancel_ordinal
                    if cancellable_nodes:
                        adapter = self._switch_adapter
                        if adapter is None:
                            raise FleetProfileUnavailable(
                                "Profile cancellation authority is unavailable",
                                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                            )
                        adapter.request_superseded_workload_cancellation_in_session(
                            session, cancellable_nodes, cancel_ordinal, now
                        )
                cancellation = FleetProfileApplicationCancellationIntent(
                    request_key=request_key,
                    actor=actor,
                    requested_at=now,
                    cause="operator",
                    workload_intent_ordinal=cancel_ordinal,
                )
                progress_data = progress.model_dump(mode="json")
                progress_data["cancellation"] = cancellation.model_dump(mode="json")
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                # Nothing fenced or issued: the application only waits (for
                # preparation, build capacity or its turn), so the core cancels it
                # at once and leaves the running workload alone.  Otherwise the
                # cancel is driven by the worker (its stops are the core's, spaced
                # and bounded), never run inside this admission transaction.
                waiting_only = ordinal is None and not has_child
                if retrying_failure:
                    # An ended load the Controller would retry is shown as queued
                    # and is cancellable like one: reopen it so the cancel (and any
                    # child it still owns) is driven to its end like a live load's.
                    self._lifecycle.reopen(
                        row,
                        now,
                        reason="Cancellation requested; reconciling issued profile effects.",
                        session=session,
                    )
                self._lifecycle.request_cancel(
                    row,
                    now,
                    reason=(
                        "Profile application cancelled before any workload "
                        "effect was issued; the running workload was not touched."
                        if waiting_only
                        else "Cancellation requested; reconciling issued profile effects."
                    ),
                    session=session,
                    run_commands=False,
                )

        if waiting_only:
            if not isinstance(plan, Residue):
                self._cancel_owned_preparations(application_id, plan, actor=actor)
            return self.application(application_id)
        adapter = self._switch_adapter
        if adapter is not None and cancellation is not None:
            request_cancellation = getattr(adapter, "request_cancellation", None)
            if callable(request_cancellation):
                request_cancellation(
                    application_id,
                    request_key=cancellation.request_key,
                    actor=cancellation.actor,
                )
        return self.application(application_id)

    def _observe_pending_cancellation(self, now: datetime) -> bool:
        """Advance one due cancellation before selecting ordinary active work."""

        adapter = self._switch_adapter
        if adapter is None:
            return False
        progress = FleetProfileApplication.progress
        cancellation = progress["cancellation"]
        cancellation_state = cancellation["state"].as_string()
        observation_due = cancellation["observation_due_at"].as_string()
        eligible = (
            FleetProfileApplication.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.NEEDS_OPERATOR,
                )
            )
            & cancellation_state.in_(fleet_profile_states.CANCEL_IN_FLIGHT)
            & (func.coalesce(observation_due, "") <= now.isoformat())
        )
        with self._sessions() as session:
            candidate = session.scalar(
                select(FleetProfileApplication)
                .where(eligible)
                .order_by(
                    FleetProfileApplication.updated_at,
                    FleetProfileApplication.created_at,
                    FleetProfileApplication.id,
                )
                .limit(1)
            )
            if candidate is None:
                return False
            application_id = candidate.id
            try:
                intent = _persisted_profile_progress(candidate).cancellation
            except FleetProfileConflict:
                intent = None
        request_cancellation = getattr(adapter, "request_cancellation", None)
        if intent is not None and callable(request_cancellation):
            request_cancellation(
                application_id,
                request_key=intent.request_key,
                actor=intent.actor,
            )
        with self._sessions.begin() as session:
            row = session.scalar(
                select(FleetProfileApplication)
                .where(eligible, FleetProfileApplication.id == application_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            plan = _persisted_profile_plan(row)
            current = _stored_progress(row)
            if isinstance(current, Residue):
                # A cancel always completes: evidence that cannot be read is an
                # unknown effect, recorded as such (the document is retained
                # untouched), not a reason to park the load for a person.  (An
                # unreadable *plan* is not needed to drive the cancel.)
                self._lifecycle.cancelled(
                    row,
                    "Profile application cancelled; its persisted effect evidence "
                    f"could not be read, so its effect is unknown: {current.note[:300]}",
                    now,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            intent = current.cancellation
            if intent is None or intent.state != LifecycleState.OBSERVING:
                return False
            if (
                intent.observation_due_at is not None
                and _aware(intent.observation_due_at) > now
            ):
                return False
            return self._advance_cancellation_in_session(
                session, row, plan, current, now
            )

    def _reconcile_selected_roster(self, now: datetime) -> bool:
        drift_signature: tuple[tuple[str, str], ...] = ()
        with self._sessions() as session:
            selected = self._selected_profile_snapshot(session)
            if selected is None or isinstance(selected, Residue):
                # A damaged selected receipt is recorded; the ordinary
                # application worker fails it without blocking unrelated work.
                return False
            roster = tuple(
                session.scalars(
                    select(AgentNode.node_id)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            roster_changed = roster != selected.roster_node_ids
            application = session.get(FleetProfileApplication, selected.application_id)
            if application is not None and application.state == "succeeded":
                drift_signature = tuple(
                    sorted(
                        (assignment.id, state.current_state)
                        for assignment in selected.intended.assignments
                        if assignment.desired_state == DesiredAssignmentState.RUNNING
                        for state in (self._assignment_state(session, assignment),)
                        if state.current_state != ObservedAssignmentState.RUNNING
                    )
                )
            if not roster_changed and not drift_signature:
                return False
            if not self._user_has_profile_authority(
                session.scalar(select(User).where(User.subject == selected.actor)),
                mutation=True,
            ):
                # Do not repeatedly preview or persist pending admissions
                # under revoked authority. The view derives the blocker from
                # this same roster mismatch and current authority state.
                return False

        # Preserve the accepted topology exactly. Preview compares it with the
        # current roster and owns the incomplete multi-Spark cleanup decision.
        assignments = tuple(selected.intended.assignments)
        preview = self.preview(
            selected.profile_id,
            execution_assignments=assignments,
            profile_name=selected.plan.profile_name,
            profile_digest=selected.intended.profile_digest,
            accepted_intent=selected.intended,
            accepted_profile_revision=selected.profile_revision,
            accepted_profile_definition=selected.plan.profile_definition,
        )
        request_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk-forge:fleet-profile-reconcile:"
                f"{selected.application_id}:{selected.generation}:"
                f"{_roster_digest(roster)}:"
                f"{hashlib.sha256(repr(drift_signature).encode()).hexdigest()}",
            )
        )
        if not preview.allowed:
            # Keep the prior generation selected. The next normal worker tick
            # rechecks the same roster change after cache, capacity or roster
            # blockers clear; no failed application can poison reconciliation.
            return False
        pending: FleetProfileApplicationView | None = None
        try:
            pending = self._create_pending_application(
                preview,
                request_key=request_key,
                actor=selected.actor,
                operation_kind="fleet-profile.apply",
                select_profile=True,
                selection_precondition=selected,
            )
            self._queue_application(
                preview,
                request_key=request_key,
                actor=selected.actor,
                operation_kind="fleet-profile.apply",
                pending_application_id=pending.id,
            )
        except FleetProfilePermissionDenied:
            # The accepted snapshot remains selected, but revoked authority
            # cannot authorize a roster effect. Retire only this unbound
            # admission receipt so it cannot become a retrying shadow intent.
            if pending is not None:
                self._discard_pending_application(pending.id)
            return False
        except (FleetProfileAdmissionBusy, FleetProfileAdmissionEffectBusy) as error:
            if pending is None:
                raise
            self._defer_pending_application(
                pending.id,
                str(error),
                code=_deferral_code(error),
                storage=_storage_wait_of(error),
            )
        except FleetProfileAdmissionStorageError as error:
            if pending is None:
                raise
            self._defer_pending_application(
                pending.id, str(error), retry_delay=timedelta(seconds=60)
            )
        except _FleetProfileSupersededIntentConflict as error:
            if pending is None:
                raise
            self._finish_pending_admission(
                pending.id,
                state=LifecycleState.SUPERSEDED,
                reason=str(error),
                code=SupersedeCode.SUPERSEDED_BY_INTENT,
            )
        except FleetProfileConflict as error:
            if pending is None:
                raise
            self._finish_pending_admission(
                pending.id,
                state=LifecycleState.FAILED,
                reason=str(error) or "Profile reconcile failed",
            )
        return True

    def tick(self) -> bool:
        """Observe one due cancellation, then advance one ordinary work item."""

        if self._switch_adapter is None:
            return False
        now = _aware(self._clock())
        if self._reconcile_selected_roster(now):
            return True
        pending_admission_observed = self._observe_pending_admissions(now)
        parked_observed = self._heal_legacy_applications(now)
        recovery_deferred = False
        cancellation_observed = self._observe_pending_cancellation(now)
        recovery = self._automatic_profile_recovery(now)
        if recovery is not None:
            application_id, actor = recovery
            request_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk-forge:profile-cache-recovery:{application_id}",
                )
            )
            try:
                self.retry(
                    application_id,
                    request_key=request_key,
                    actor=actor,
                    automatic_cache_recovery=True,
                )
            except _FleetProfileRecoveryBindingConflict as error:
                with self._sessions.begin() as session:
                    row = session.get(
                        FleetProfileApplication, application_id, with_for_update=True
                    )
                    if row is not None and self._retry_eligible(session, row):
                        self._park_for_retry(
                            row,
                            _persisted_profile_progress(row),
                            [
                                make_blocker(
                                    ProfileReasonCode.RECOVERY_CACHE_PENDING, str(error)
                                )
                            ],
                            because=error,
                        )
                        recovery_deferred = True
                # No replacement intent or unknown-output build was admitted.
                # The existing backoff revisits this receipt after cache repair.
            except (FleetProfileConflict, FleetProfilePermissionDenied) as error:
                with self._sessions.begin() as session:
                    row = session.get(
                        FleetProfileApplication, application_id, with_for_update=True
                    )
                    disposition = retry_disposition_of(error)
                    if (
                        row is not None
                        and disposition == RETRY_SUPERSEDE
                        and self._lifecycle.retry_pending(row)
                    ):
                        # What waiting cannot change (a stale review, a lost
                        # selection, a superseded intent) never becomes valid:
                        # end the application so the client re-reviews and
                        # re-submits instead of parking it forever.
                        self._lifecycle.supersede(
                            row,
                            str(error),
                            _aware(self._clock()),
                            code=error.supersede_code
                            if isinstance(error, FleetProfileConflict)
                            else SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
                            session=session,
                        )
                        recovery_deferred = True
                    elif (
                        row is not None
                        and disposition == RETRY_WAIT
                        and not is_security_failure(error_code(error))
                        and self._retry_eligible(session, row)
                    ):
                        self._park_for_retry(
                            row,
                            _persisted_profile_progress(row),
                            [
                                make_blocker(
                                    error_code(error)
                                    or ProfileReasonCode.RETRY_CONFLICT,
                                    str(error) or "The profile could not be retried",
                                )
                            ],
                            because=error,
                        )
                        recovery_deferred = True
            else:
                return True
        with self._sessions.begin() as session:
            cancellation_state = func.coalesce(
                FleetProfileApplication.progress["cancellation"]["state"].as_string(),
                "",
            )
            admission_pending = func.coalesce(
                FleetProfileApplication.progress["admission_pending"].as_boolean(),
                False,
            )
            row = session.scalar(
                select(FleetProfileApplication)
                .where(
                    FleetProfileApplication.state.in_(("queued", "running")),
                    cancellation_state.not_in(fleet_profile_states.CANCEL_IN_FLIGHT),
                    admission_pending.is_(False),
                )
                .order_by(
                    FleetProfileApplication.created_at, FleetProfileApplication.id
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return (
                    pending_admission_observed
                    or cancellation_observed
                    or parked_observed
                    or recovery_deferred
                )
            plan = _persisted_profile_plan(row)
            stored = _stored_progress(row)
            if isinstance(plan, Residue) or isinstance(stored, Residue):
                # Damaged evidence is retired as unknown, never parked and never
                # failed for a person: the load ends cancelled with its effect
                # unknown (what it issued keeps its own lifecycle and receipts),
                # and the selected profile's reconciliation issues a fresh one.
                damaged = plan if isinstance(plan, Residue) else stored
                assert isinstance(damaged, Residue)
                self._lifecycle.cancelled(
                    row,
                    "Profile application retired; its persisted evidence could "
                    f"not be read ({damaged.kind}), so its effect is unknown",
                    now,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            progress = stored
            if (
                progress.cancellation is not None
                and progress.cancellation.state == LifecycleState.OBSERVING
            ):
                # Defensive parity with the SQL exclusion above. Never let a
                # malformed query or dialect quirk monopolize ordinary work.
                return cancellation_observed
            if not self._application_is_current_selection(session, row, progress):
                self._lifecycle.supersede(
                    row,
                    "Profile order was replaced by a newer fleet profile load; "
                    "issued effects retain their cancellation receipts",
                    now,
                    code=SupersedeCode.SUPERSEDED_BY_INTENT,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            if self._superseding_intent(session, row, progress):
                self._lifecycle.supersede(
                    row,
                    "Profile order was replaced by a changed profile or later "
                    "scoped intent; issued effects retain their own cancellation receipts",
                    now,
                    code=SupersedeCode.SUPERSEDED_BY_INTENT,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            steps = [step.model_dump(mode="json") for step in plan.steps]
            if row.current_operation_id:
                try:
                    child = self._switch_adapter.advance(
                        row.current_operation_id, session=session
                    )
                except (KeyError, RuntimeError, ValueError) as error:
                    # A damaged persisted document is retained as the evidence of
                    # what was issued (a missing child record is reconciled before
                    # this point, by issuing the step again).
                    self._lifecycle.fail(
                        row,
                        str(error) or "Child operation is unavailable",
                        now,
                        session=session,
                    )
                    return True
                # The adapter may have checkpointed its child in this same
                # row/transaction. Read that receipt before mirroring progress.
                progress = _persisted_profile_progress(row)
                progress_data = progress.model_dump(mode="json")
                if child.progress is not None:
                    progress_data["child_progress"] = child.progress.model_dump(
                        mode="json"
                    )
                if child.state in _CHILD_PENDING_STATES:
                    # A live child that keeps retrying a phase is a stall the
                    # application names, with its cause, until the child moves.
                    held = [
                        item
                        for item in progress.blockers
                        if item.code == PHASE_RETRY_CODE
                    ]
                    if child.stalls != held:
                        progress_data["blockers"] = [
                            item.model_dump(mode="json")
                            for item in bound_blockers(
                                [
                                    *(
                                        item
                                        for item in progress.blockers
                                        if item.code != PHASE_RETRY_CODE
                                    ),
                                    *child.stalls,
                                ]
                            )
                        ]
                    if child.stalls or held:
                        reason = (
                            (child.status_reason or "")[:512] if child.stalls else None
                        )
                        if row.status_reason != reason:
                            row.status_reason = reason
                if progress_data != progress.model_dump(mode="json"):
                    progress = read_stored_model(
                        FleetProfileApplicationProgress,
                        canonical_message(progress_data),
                        strict=True,
                        from_json=True,
                    )
                    row.progress = progress.model_dump(mode="json")
                if child.state in _CHILD_PENDING_STATES:
                    if row.state == "running" and not session.is_modified(row):
                        return (
                            pending_admission_observed
                            or cancellation_observed
                            or parked_observed
                            or recovery_deferred
                        )
                    self._lifecycle.project(
                        row, now, state=_LifecycleState.RUNNING, session=session
                    )
                    return True
                if child.state in _CHILD_FAILED_STATES:
                    # The load follows its child: a child that ended is not a wait
                    # for a person (none exists), it is a definite end.
                    self._lifecycle.fail(
                        row,
                        child.status_reason
                        or f"Profile step {row.current_step + 1} ended in {child.state}",
                        now,
                        session=session,
                    )
                    return True
                if child.state != "succeeded":
                    self._lifecycle.fail(
                        row,
                        f"Profile step {row.current_step + 1} returned unsupported "
                        f"state {child.state}",
                        now,
                        session=session,
                    )
                    return True
                progress_data = progress.model_dump(mode="json")
                results = dict(progress_data.get("step_results", {}))
                # The receipt names the *plan* step it completed.  Copying the
                # child's own kind recorded "recipe.uninstall" for an
                # "uninstall" step and nothing at all for a switch-adapter
                # child, so no reader could match a receipt to its plan step.
                completed_step = steps[row.current_step]
                child_result = FleetProfileStepResult(
                    operation_id=child.id,
                    kind=completed_step["kind"],
                    result=child.result,
                )
                results[str(row.current_step)] = child_result.model_dump(mode="json")
                progress_data["step_results"] = results
                progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                )
                row.progress = progress.model_dump(mode="json")
                row.current_operation_id = None
                row.current_step += 1
            if row.current_step >= len(steps):
                self._lifecycle.succeed(row, now, reason=None, session=session)
                progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(
                        {
                            **progress.model_dump(mode="json"),
                            "completed_steps": len(steps),
                            "total_steps": len(steps),
                        }
                    ),
                    strict=True,
                    from_json=True,
                )
                row.progress = progress.model_dump(mode="json")
                row.result = FleetProfileApplicationResult(
                    changed=bool(steps), completed_steps=len(steps)
                ).model_dump(mode="json")
                row.updated_at = now
                return True
            raw_step = steps[row.current_step]
            if not isinstance(raw_step, Mapping):
                self._lifecycle.fail(
                    row,
                    "Persisted Fleet profile step is invalid",
                    now,
                    session=session,
                )
                return True
            self._lifecycle.project(
                row, now, state=_LifecycleState.RUNNING, session=session
            )
            progress = read_stored_model(
                FleetProfileApplicationProgress,
                canonical_message(
                    {
                        **progress.model_dump(mode="json"),
                        "completed_steps": row.current_step,
                        "total_steps": len(steps),
                        "current_label": raw_step.get("label", "Applying profile"),
                    }
                ),
                strict=True,
                from_json=True,
            )
            row.progress = progress.model_dump(mode="json")
            row.updated_at = now
            step = dict(raw_step)
            application_id = row.id
            step_index = row.current_step
            actor = row.actor

        try:
            started = self._start_step(
                application_id,
                step_index,
                step,
                actor=actor,
            )
        except (
            KeyError,
            RecipeOperationConflict,
            FleetProfileConflict,
            RuntimeError,
            ValueError,
        ) as error:
            with self._sessions.begin() as session:
                failed = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if failed is not None and failed.state in {"queued", "running"}:
                    try:
                        cancellation = _persisted_profile_progress(failed).cancellation
                    except FleetProfileConflict:
                        cancellation = None
                    if cancellation is not None:
                        # Cancellation can win while the stable child request is
                        # outside the parent transaction. Keep the durable cancel
                        # intent and let the next worker pass reconcile whether
                        # the child was issued; a start error cannot resurrect or
                        # fail that request on its behalf.
                        failed.status_reason = (
                            "Cancellation is reconciling the profile child: "
                            + (str(error)[:360] or "child start was interrupted")
                        )[:512]
                    elif isinstance(error, FleetProfileReviewStale):
                        # The reviewed plan no longer matches what the child
                        # would do: retrying can never succeed. End it so the
                        # client reviews and submits again.
                        self._lifecycle.supersede(
                            failed,
                            str(error),
                            _aware(self._clock()),
                            code=SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
                            session=session,
                        )
                    else:
                        self._lifecycle.fail(
                            failed,
                            str(error) or "Profile operation could not be started",
                            _aware(self._clock()),
                            session=session,
                        )
                    failed.updated_at = _aware(self._clock())
            return True
        if isinstance(started, Residue):
            return self._step_unissued(application_id, started)
        operation_id, synchronous, child = started
        with self._sessions.begin() as session:
            current = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if current is None or current.state not in {"queued", "running"}:
                return True
            try:
                current_progress = _persisted_profile_progress(current)
                progress_data = current_progress.model_dump(mode="json")
                if synchronous:
                    current.current_step += 1
                    progress_data["completed_steps"] = current.current_step
                else:
                    current.current_operation_id = operation_id
                    progress_data["child_source"] = "switch-adapter"
                    if (
                        isinstance(child, FleetProfileChildOperation)
                        and child.progress is not None
                    ):
                        progress_data["child_progress"] = child.progress.model_dump(
                            mode="json"
                        )
                current.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
            except FleetProfileConflict as error:
                self._lifecycle.fail(
                    current, str(error), _aware(self._clock()), session=session
                )
            current.updated_at = _aware(self._clock())
        return True

    def _step_unissued(self, application_id: str, residue: Residue) -> bool:
        """A step could not be issued: wait for the evidence, or retire the load.

        Evidence that is only *not there yet* (an executor not bound) leaves the
        load where it is and is looked at again on the next pass.  Evidence that is
        damaged or no longer matches retires the load as superseded with its effect
        unknown (what it already issued keeps its own lifecycle), so the selected
        profile's reconciliation can issue a fresh one.
        """

        if residue.reason is BookkeepingReason.EVIDENCE_UNAVAILABLE:
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if row is not None and row.state in {
                    _LifecycleState.QUEUED,
                    _LifecycleState.RUNNING,
                }:
                    row.status_reason = (
                        f"Waiting to issue the next step ({residue.note})"
                    )[:512]
                    row.updated_at = _aware(self._clock())
            return False
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is not None and row.state in {
                _LifecycleState.QUEUED,
                _LifecycleState.RUNNING,
            }:
                self._lifecycle.cancelled(
                    row,
                    "Profile order retired: its stored evidence could not be "
                    f"read ({residue.note}); its effect is unknown",
                    _aware(self._clock()),
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
        return True

    def _replan_blocked_application(
        self, application_id: str, blocked: FleetProfilePreview, actor: str
    ) -> FleetProfilePreview | None:
        """Bind a waiting load to its plan once nothing blocks it any more.

        Returns the admissible plan, now bound to the accepted intent, or
        ``None`` while conditions still block it (the reasons are recorded and
        the preparation it needs is requested again).
        """

        try:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is None:
                    return None
                intended = self._intended_profile(row, session=session)
            if isinstance(intended, Residue):
                self._finish_pending_admission(
                    application_id,
                    state=_CANCELLED_OPERATION,
                    reason="Profile admission retired: its accepted intent could "
                    "not be read; load the profile again",
                )
                return None
            fresh = self.preview(
                blocked.profile_id,
                execution_assignments=tuple(intended.assignments),
                profile_name=blocked.profile_name,
                profile_digest=intended.profile_digest,
                accepted_intent=intended,
                accepted_profile_revision=blocked.profile_revision,
                accepted_profile_definition=blocked.profile_definition,
                excluded_application_id=application_id,
            )
        except (FleetProfileConflict, KeyError) as error:
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.FAILED,
                reason=str(error) or "Profile admission could not be resumed",
            )
            return None
        if not fresh.allowed:
            if not _profile_preview_is_waitable(fresh):
                self._finish_pending_admission(
                    application_id,
                    state=LifecycleState.FAILED,
                    reason="Fleet profile intent contains a security or contract blocker",
                )
                return None
            blockers = _preview_blockers(fresh) + self._request_preparations(
                fresh, actor=actor
            )
            self._defer_pending_application(
                application_id,
                "Waiting for current Fleet conditions: "
                + "; ".join(item.code for item in blockers[:8])
                + ".",
                blockers=blockers,
                storage=_storage_wait_of_preview(fresh),
                wait_for_space=True,
            )
            return None
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return None
            progress = _persisted_profile_progress(row)
            if not _owns_pending_admission(row, progress):
                return None
            pending_digest = _digest(
                {
                    "schema_version": 2,
                    "reconciliation_digest": fresh.plan_digest,
                    "retry_of_application_id": None,
                    "request_key": row.request_key,
                }
            )
            assert progress.intended_profile is not None
            row.plan = fresh.model_copy(
                update={"plan_digest": pending_digest}
            ).model_dump(mode="json")
            row.plan_digest = pending_digest
            row.progress = _progress_with_blockers(
                progress.model_copy(
                    update={
                        "intended_profile": progress.intended_profile.model_copy(
                            update={"reviewed_plan_digest": fresh.plan_digest}
                        ),
                        "total_steps": len(fresh.steps),
                    }
                ),
                [],
            )
            row.updated_at = _aware(self._clock())
        return fresh

    def _follow_newest_recipe_revisions(self, application_id: str, actor: str) -> bool:
        """Re-plan a load that has not started anything on the newest recipes.

        A load waiting to be admitted (for example for a runtime image) has
        touched no Spark yet, so nothing running is swapped by following the
        recipe: profile loads always use the newest revision.  The saved
        profile must be the one that was accepted; a changed profile is a new
        intent and keeps superseding this one.  Returns ``True`` when the
        application was re-planned against the newest revisions and the current
        Fleet.
        """

        try:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is None:
                    return False
                progress = _persisted_profile_progress(row)
                if (
                    not _owns_pending_admission(row, progress)
                    or row.current_operation_id is not None
                    or row.current_step != 0
                ):
                    return False
                intended = self._intended_profile(row, session=session)
                if isinstance(intended, Residue):
                    return False
                profile = session.get(FleetProfile, row.profile_id)
                if (
                    profile is None
                    or _digest(_profile_document(profile)) != intended.profile_digest
                ):
                    return False
                newest = self._execution_assignments(session, profile)
                accepted = {item.id: item for item in intended.assignments}
                if set(accepted) != {item.id for item in newest}:
                    return False
                moved = [
                    item
                    for item in newest
                    if item.recipe_revision_id != accepted[item.id].recipe_revision_id
                ]
                if not moved:
                    return False
                versions = []
                for item in moved:
                    revision = session.get(
                        CatalogDocumentRevision, item.recipe_revision_id
                    )
                    release = revision.document.get("release") if revision else None
                    version = (
                        release.get("version") if isinstance(release, dict) else None
                    )
                    versions.append(
                        f"{item.recipe_title} {version}"
                        if isinstance(version, str)
                        else item.recipe_title
                    )
                followed = intended.model_copy(
                    update={"assignments": sorted(newest, key=lambda item: item.id)}
                )
                plan = _persisted_profile_plan(row)
                if isinstance(plan, Residue):
                    return False
                fresh = self.preview(
                    row.profile_id,
                    execution_assignments=tuple(followed.assignments),
                    profile_name=plan.profile_name,
                    profile_digest=followed.profile_digest,
                    accepted_intent=followed,
                    accepted_profile_revision=plan.profile_revision,
                    accepted_profile_definition=plan.profile_definition,
                    excluded_application_id=application_id,
                )
        except (FleetProfileConflict, ValidationError, KeyError):
            # The ordinary admission path reports whatever is unresumable.
            return False
        blockers: list[OperationBlocker] = []
        if not fresh.allowed and _profile_preview_is_waitable(fresh):
            blockers = _preview_blockers(fresh) + self._request_preparations(
                fresh, actor=actor
            )
        reason = (
            f"Recipe updated to {', '.join(versions)}; re-planned."
            if versions
            else "Recipe updated; re-planned."
        )
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return False
            progress = _persisted_profile_progress(row)
            if (
                not _owns_pending_admission(row, progress)
                or row.current_operation_id is not None
                or row.current_step != 0
                or progress.intended_profile != intended
            ):
                return False
            pending_digest = _digest(
                {
                    "schema_version": 2,
                    "reconciliation_digest": fresh.plan_digest,
                    "retry_of_application_id": None,
                    "request_key": row.request_key,
                }
            )
            row.plan = fresh.model_copy(
                update={"plan_digest": pending_digest}
            ).model_dump(mode="json")
            row.plan_digest = pending_digest
            row.progress = _progress_with_blockers(
                progress.model_copy(
                    update={
                        "intended_profile": followed.model_copy(
                            update={"reviewed_plan_digest": fresh.plan_digest}
                        ),
                        "total_steps": len(fresh.steps),
                    }
                ),
                blockers,
                admission_pending=True,
                admission_attempt=0,
                admission_retry_at=now.isoformat(),
            )
            self._lifecycle.project(
                row, now, state=_LifecycleState.QUEUED, reason=reason, session=session
            )
        return True

    def _observe_pending_admissions(self, now: datetime) -> bool:
        """Retry reviewed applications that could not acquire admission locks."""

        candidate: tuple[str, str, str, FleetProfilePreview] | None = None
        # A waiting load follows a newer recipe revision at once, not when its
        # own retry backoff falls due.
        with self._sessions() as session:
            waiting = tuple(
                session.execute(
                    select(FleetProfileApplication.id, FleetProfileApplication.actor)
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                            )
                        ),
                        FleetProfileApplication.current_operation_id.is_(None),
                        FleetProfileApplication.current_step == 0,
                    )
                    .order_by(FleetProfileApplication.created_at)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
            )
        for waiting_id, waiting_actor in waiting:
            if self._follow_newest_recipe_revisions(waiting_id, waiting_actor):
                return True
        with self._sessions() as session:
            progress_document = FleetProfileApplication.progress
            admission_pending = (
                (func.json_typeof(progress_document["admission_pending"]) == "boolean")
                & (progress_document["admission_pending"].as_string() == "true")
                if session.get_bind().dialect.name == "postgresql"
                else progress_document["admission_pending"].as_boolean().is_(True)
            )
            retry_at = func.replace(
                progress_document["admission_retry_at"].as_string(),
                "Z",
                "+00:00",
            )
            retry_cutoff = TypeAdapter(datetime).dump_python(_aware(now), mode="json")
            if not isinstance(retry_cutoff, str):
                raise InvalidType(
                    "profile admission retry cutoff is not a string",
                    reason=InvalidRequestReason.MALFORMED,
                )
            retry_cutoff_text = retry_cutoff.replace("Z", "+00:00")
            rows = session.scalars(
                select(FleetProfileApplication)
                .where(
                    FleetProfileApplication.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                        )
                    ),
                    admission_pending,
                    or_(retry_at.is_(None), retry_at <= retry_cutoff_text),
                )
                .order_by(
                    FleetProfileApplication.updated_at.desc(),
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
            )
            for row in rows:
                progress = _persisted_profile_progress(row)
                plan = _persisted_profile_plan(row)
                if isinstance(plan, Residue):
                    continue
                if not _owns_pending_admission(row, progress):
                    continue
                # The SQL text predicate narrows the bounded batch without a
                # cast that malformed historical JSON could make fail. The
                # typed timestamp remains the actual retry authority.
                if progress.admission_retry_at is not None and _aware(
                    progress.admission_retry_at
                ) > _aware(now):
                    continue
                candidate = (row.id, row.request_key, row.actor, plan)
                break
        if candidate is None:
            return False
        application_id, request_key, actor, plan = candidate
        if not plan.allowed:
            # The accepted intent was blocked when it was reviewed: plan it
            # again against current conditions (asking for the preparation it
            # needs) instead of replaying a plan that could never be admitted.
            replanned = self._replan_blocked_application(application_id, plan, actor)
            if replanned is None:
                return True
            plan = replanned
        try:
            # The persisted plan is already bound to the execution request.
            # Admission consumes the original reviewed identity and binds it
            # once; reusing the execution digest here would hash it twice.
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is None:
                    return False
                intended = self._intended_profile(row, session=session)
            if isinstance(intended, Residue):
                self._finish_pending_admission(
                    application_id,
                    state=_CANCELLED_OPERATION,
                    reason="Profile admission retired: its accepted intent could "
                    "not be read; load the profile again",
                )
                return True
            plan = plan.model_copy(
                update={"plan_digest": intended.reviewed_plan_digest}
            )
            self._prepare_pending_admission(application_id, plan)
            self._queue_application(
                plan,
                request_key=request_key,
                actor=actor,
                operation_kind="fleet-profile.apply",
                pending_application_id=application_id,
            )
        except (
            FleetProfileAdmissionBusy,
            FleetProfileAdmissionEffectBusy,
            FleetProfileAdmissionStorageError,
        ) as error:
            self._defer_pending_application(
                application_id,
                str(error),
                code=_deferral_code(error),
                storage=_storage_wait_of(error),
            )
            return True
        except FleetProfileStalePlanConflict as error:
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.SUPERSEDED,
                reason=f"Pending profile intent was superseded: {error}",
                code=SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
            )
            return True
        except (FleetProfileConflict, FleetProfilePermissionDenied, KeyError) as error:
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.FAILED,
                reason=str(error) or "Profile admission could not be resumed",
            )
            return True
        return True

    def _finish_pending_admission(
        self,
        application_id: str,
        *,
        state: FleetProfileOperationState,
        reason: str,
        code: FleetProfileSupersedeCode | None = None,
    ) -> None:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return
            progress = _persisted_profile_progress(row)
            if not _owns_pending_admission(row, progress):
                return
            row.progress = _progress_with_blockers(
                progress,
                progress.blockers,
                admission_pending=False,
                admission_retry_at=None,
            )
            if state == "superseded":
                assert code is not None
                self._lifecycle.supersede(row, reason, now, code=code, session=session)
            elif state == "cancelled":
                self._lifecycle.cancelled(row, reason, now, session=session)
            else:
                self._lifecycle.fail(row, reason, now, session=session)

    def _heal_legacy_applications(self, now: datetime) -> bool:
        """Re-evaluate a legacy ``waiting-for-operator`` load: it runs again.

        An older Controller mirrored a child that waited for a person onto the load
        and left it parked: the ordinary advancement never revisited it.  No profile
        (or Run/Switch) action exists, so nothing can end that wait.  The load is
        returned to the work queue (rules 1 and 3): the next ordinary tick observes
        its exact child and records whatever that child concluded, exactly as for any
        running load; one still being admitted retries its admission.  Nothing is
        issued here and no child is touched, so running workloads are never
        disturbed.  Bounded per tick, and idempotent after a restart.
        """

        with self._sessions.begin() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(LifecycleState.NEEDS_OPERATOR)
                        ),
                        func.coalesce(
                            FleetProfileApplication.progress["cancellation"][
                                "state"
                            ].as_string(),
                            "",
                        ).not_in(fleet_profile_states.CANCEL_IN_FLIGHT),
                    )
                    .order_by(
                        FleetProfileApplication.created_at,
                        FleetProfileApplication.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
            )
            for row in rows:
                self._lifecycle.heal(row, now, session=session)
            legacy = tuple(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        or_(
                            *(
                                FleetProfileApplication.status_reason.startswith(prefix)
                                for prefix in LEGACY_SUPERSEDED_PREFIXES
                            )
                        ),
                        FleetProfileApplication.state.in_(("failed", "cancelled")),
                    )
                    .order_by(
                        FleetProfileApplication.created_at,
                        FleetProfileApplication.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
            )
            relabelled = [self._lifecycle.heal_superseded(row, now) for row in legacy]
            return bool(rows) or any(relabelled)

    @staticmethod
    def _defer_cancellation_observation(
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        now: datetime,
        *,
        due_at: datetime | None = None,
    ) -> None:
        """Persist the next bounded observation time for a pending owner."""

        if progress.cancellation is None:
            return
        due = _aware(due_at) if due_at is not None else None
        if due is None or due <= now:
            due = now + timedelta(seconds=_CANCELLATION_OBSERVATION_SECONDS)
        progress_data = progress.model_dump(mode="json")
        cancellation_data = dict(progress_data["cancellation"])
        cancellation_data["observation_due_at"] = due.isoformat()
        progress_data["cancellation"] = cancellation_data
        row.progress = read_stored_model(
            FleetProfileApplicationProgress,
            canonical_message(progress_data),
            strict=True,
            from_json=True,
        ).model_dump(mode="json")

    def _advance_cancellation_in_session(
        self,
        session: Session,
        row: FleetProfileApplication,
        plan: FleetProfilePreview | Residue,
        progress: FleetProfileApplicationProgress,
        now: datetime,
    ) -> bool:
        """Drive one pass of a cancel: the core stops and observes, and it completes.

        The children are stopped by :meth:`_stop_children` (the same reconcile the
        worker always did, one pass per due observation); the core decides the rest:
        when nothing is left the load is ``cancelled``, and when the budget is spent
        it is ``cancelled`` anyway with the children's effect recorded unknown and
        their operation ids kept.  A cancel never parks the load for a person.
        """

        del plan, progress
        settled = self._lifecycle.tick_cancel(
            row,
            now,
            terminal_reason=(
                "Profile application cancelled after issued effects were reconciled"
            ),
            session=session,
        )
        return bool(settled.changed or settled.commands)

    def _stop_children(self, session: Session, row: FleetProfileApplication) -> bool:
        """One idempotent pass of the cancel's stop: ``True`` when nothing is left.

        Reconciles the exact current child (a Run/Switch operation under the
        profile's switch-adapter document), then the older agent effects fenced by
        the cancel's workload intent.  Whatever cannot be confirmed yet is a wait the
        core observes again (and bounds), recorded for the operator view; nothing
        here is a refusal.
        """

        adapter = self._switch_adapter
        now = _aware(self._clock())
        progress = _stored_progress(row)
        if isinstance(progress, Residue):
            # Unreadable evidence is an unknown, not a verdict: it is observed
            # again, and the cancel's budget ends it.
            return False
        intent = progress.cancellation
        if intent is None:
            return True
        if adapter is None:
            return False
        if progress.switch_adapter is not None:
            try:
                child = adapter.advance(row.id, session=session)
            except (KeyError, RuntimeError, TypeError, ValueError):
                self._defer_cancellation_observation(row, progress, now)
                row.status_reason = (
                    "Cancellation is waiting for its profile switch child to be "
                    "reconciled by the Run/Switch owner"
                )
                row.updated_at = now
                return False
            progress = _persisted_profile_progress(row)
            progress_data = progress.model_dump(mode="json")
            if child.progress is not None:
                progress_data["child_progress"] = child.progress.model_dump(mode="json")
            row.progress = read_stored_model(
                FleetProfileApplicationProgress,
                canonical_message(progress_data),
                strict=True,
                from_json=True,
            ).model_dump(mode="json")
            progress = _persisted_profile_progress(row)
            if child.state == "cancelled" or (
                progress.cancellation is not None
                and progress.cancellation.state == "cancelled"
            ):
                return True
            self._defer_cancellation_observation(
                row,
                progress,
                now,
                due_at=(
                    progress.switch_adapter.observation_due_at
                    if progress.switch_adapter is not None
                    else None
                ),
            )
            if child.state in _CHILD_PENDING_STATES or child.state in job_states.words(
                LifecycleState.NEEDS_OPERATOR
            ):
                row.status_reason = (
                    child.status_reason
                    or "Cancellation is waiting for the active profile child"
                )[:512]
            else:
                row.status_reason = (
                    f"Cancellation is waiting for the Run/Switch owner to reconcile "
                    f"child state {child.state}"
                )[:512]
            row.updated_at = now
            return False

        if row.current_operation_id is not None:
            self._defer_cancellation_observation(row, progress, now)
            row.status_reason = (
                "Cancellation is waiting for the recorded profile child identity "
                "to become available"
            )
            row.updated_at = now
            return False

        # (An unreadable plan falls back to the scope the row declared.)
        scope = _application_effect_scope(row)
        if scope and intent.workload_intent_ordinal is not None:
            try:
                effects = AgentJobService.assess_superseded_agent_effects_in_session(
                    session, scope, intent.workload_intent_ordinal, now
                )
            except (TypeError, ValueError) as error:
                self._defer_cancellation_observation(row, progress, now)
                row.status_reason = (
                    "Cancellation is waiting on issued-effect evidence it cannot "
                    f"read yet: {str(error)[:360]}"
                )[:512]
                row.updated_at = now
                return False
            if effects:
                deadline = min(effect.observation_deadline for effect in effects)
                due = min(effect.observe_due_at for effect in effects)
                operation_ids = sorted(effect.operation_id for effect in effects)
                progress_data = progress.model_dump(mode="json")
                cancellation_data = dict(progress_data["cancellation"])
                cancellation_data.update(
                    {
                        "pending_operation_ids": operation_ids,
                        "observation_due_at": due.isoformat(),
                        "observation_deadline_at": deadline.isoformat(),
                    }
                )
                progress_data["cancellation"] = cancellation_data
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                owner = (
                    "waiting for issued agent cancellation receipts"
                    if now < deadline
                    else "issued agent cancellation receipts are overdue; its "
                    "stop budget will end the cancel"
                )
                row.status_reason = f"Cancellation {owner}: " + ", ".join(operation_ids)
                row.updated_at = now
                return False

        # No workload intent left to settle and no child: nothing is left.
        return True

    def _observe_children(
        self, session: Session, row: FleetProfileApplication
    ) -> _LifecycleEffect:
        """Read-only: whether anything of the cancelled load's children is left."""

        try:
            progress = _persisted_profile_progress(row)
        except FleetProfileConflict:
            return _LifecycleEffect.UNKNOWN
        children = self._lifecycle.bound(session).children_of(session, row)
        state = self._lifecycle.aggregate_state(children)
        if state is not None and state in {
            _LifecycleState.SUCCEEDED,
            _LifecycleState.FAILED,
            _LifecycleState.CANCELLED,
        }:
            return _LifecycleEffect.STOPPED
        if progress.cancellation is None:
            return _LifecycleEffect.NONE
        return _LifecycleEffect.UNKNOWN

    def _finish_cancel_records(
        self, session: Session, row: FleetProfileApplication, residue: bool
    ) -> None:
        """Close the records of a cancel that ended: the cancellation, the child and
        the result.  The state itself was written by the lifecycle adapter."""

        del session
        now = _aware(self._clock())
        progress = _persisted_profile_progress(row)
        progress_data = progress.model_dump(mode="json")
        cancellation_data = dict(progress_data["cancellation"] or {})
        cancellation_data.update(
            {
                # The pending operation ids stay when the stop was never confirmed:
                # they are the residue record.  A confirmed stop clears them.
                "observation_due_at": None,
                "observation_deadline_at": None,
            }
        )
        if not residue:
            cancellation_data["pending_operation_ids"] = []
        progress_data["cancellation"] = cancellation_data
        cancellation_state(progress_data, "cancelled")
        document = progress_data.get("switch_adapter")
        if isinstance(document, dict) and document.get("state") not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            document = dict(document)
            doc_state(document, "cancelled")
            if not residue:
                # A stop that was never confirmed keeps its child's identity: it is
                # the evidence of what may remain.
                document["active_operation_id"] = None
                document["active_kind"] = None
            progress_data["switch_adapter"] = document
        row.progress = read_stored_model(
            FleetProfileApplicationProgress,
            canonical_message(progress_data),
            strict=True,
            from_json=True,
        ).model_dump(mode="json")
        row.current_operation_id = None
        row.result = {
            "changed": bool(progress.completed_steps or progress.step_results),
            "completed_steps": min(progress.completed_steps, row.current_step),
        }
        row.updated_at = now

    def _recovery_wanted(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Whether the Controller will retry this failed application by itself.

        One owner for the question, shared by the recovery scan and by every
        view: an application that will be retried is waiting, not failed.
        """

        return (
            self._recovery_possible(session, row, progress)
            and self._repeated_failure(session, row, progress) is None
        )

    def _recovery_possible(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Whether this failed application is the current, replayable intent."""

        adapter = self._switch_adapter
        if (
            adapter is None
            or progress.intended_profile is None
            or adapter.recovery_refused(row.id, session=session)
        ):
            return False
        current_scope = tuple(
            session.scalars(
                select(AgentNode.node_id)
                .where(AgentNode.revoked_at.is_(None))
                .order_by(AgentNode.node_id)
            )
        )
        return tuple(
            progress.intended_profile.scope.node_ids
        ) == current_scope and self._retry_eligible(session, row)

    def _repeated_failure(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> OperationBlocker | None:
        """The evidence that this load keeps failing the same way, if it does.

        Retries are for causes that change.  A start that fails with the same
        typed cause ``RECOVERY.max_failures`` times in a row (this attempt and the
        ones its retry lineage already recorded) is deterministic: another attempt
        repeats it and re-launches the workload each time.  Read-only (views call
        it); the recovery scan records the ending once.
        """

        if row.state not in job_states.words(LifecycleState.FAILED):
            return None
        recorded = next(
            (
                blocker
                for blocker in progress.blockers
                if blocker.code == PROFILE_REPEATED_FAILURE_CODE
            ),
            None,
        )
        if recorded is not None:
            return recorded
        adapter = self._switch_adapter
        if adapter is None:
            return None
        signature = adapter.failure_signature(row.id, session=session)
        if signature is None:
            return None
        count = 1
        cursor = progress.retry_of_application_id
        while count < RECOVERY.max_failures and cursor is not None:
            ancestor = session.get(FleetProfileApplication, cursor)
            if (
                ancestor is None
                or adapter.failure_signature(ancestor.id, session=session) != signature
            ):
                break
            count += 1
            try:
                cursor = _persisted_profile_progress(ancestor).retry_of_application_id
            except FleetProfileConflict:
                break
        if count < RECOVERY.max_failures:
            return None
        cause = signature.partition("\n")[0].rpartition("|")[2] or signature
        return make_blocker(
            PROFILE_REPEATED_FAILURE_CODE,
            f"Failed the same way {count} times in a row; not retrying: {cause}",
            severity="error",
            node_ids=progress.intended_profile.scope.node_ids
            if progress.intended_profile is not None
            else (),
        )

    def _end_repeated_failures(
        self, ended: Sequence[tuple[str, OperationBlocker]], now: datetime
    ) -> None:
        """Record, once, that these loads stop retrying (they stay ``failed``)."""

        for application_id, blocker in ended:
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if row is None or row.state not in job_states.words(
                    LifecycleState.FAILED
                ):
                    continue
                progress = _persisted_profile_progress(row)
                if any(
                    item.code == PROFILE_REPEATED_FAILURE_CODE
                    for item in progress.blockers
                ):
                    continue
                if self._lifecycle.end_retry(
                    row,
                    blocker.detail,
                    now,
                    progress=_progress_with_blockers(progress, [blocker]),
                    session=session,
                ):
                    _LOGGER.warning(
                        "profile application %s: %s", application_id, blocker.detail
                    )

    def _presented_state(
        self, row: FleetProfileApplication, progress: FleetProfileApplicationProgress
    ) -> tuple[FleetProfileOperationState, datetime | None]:
        """The state to show, and when the next attempt is due.

        A failed application the Controller will retry is ``queued`` with its
        next attempt time; ``failed`` is reserved for applications that stay so.
        """

        state = _OPERATION_STATE_ADAPTER.validate_python(row.state, strict=True)
        if progress.cancellation is not None:
            return state, None
        if state == LifecycleState.FAILED:
            session = object_session(row)
            try:
                retrying = session is not None and self._recovery_wanted(
                    session, row, progress
                )
            except (FleetProfileConflict, ValidationError, TypeError, ValueError):
                retrying = False
            if retrying:
                # Automatic recovery always has a next attempt; name it even
                # when the failure recorded no explicit due time.
                return (
                    LifecycleState.QUEUED,
                    progress.retry_due_at
                    or FleetProfileAdapter.next_retry(
                        row.id, progress.attempt, _aware(row.updated_at)
                    ),
                )
            return state, None
        if state == LifecycleState.QUEUED and progress.admission_pending:
            return state, progress.admission_retry_at
        return state, None

    def _automatic_profile_recovery(self, now: datetime) -> tuple[str, str] | None:
        """Find one current profile intent that can safely be reconciled again."""

        ended: list[tuple[str, OperationBlocker]] = []
        try:
            with self._sessions() as session:
                adapter = self._switch_adapter
                if adapter is None:
                    return None
                # Only due rows are scanned (the typed timestamp below remains the
                # authority), and the bounded batch walks all due rows round-robin
                # so a refused or ineligible row cannot starve an eligible one.
                retry_at = func.replace(
                    FleetProfileApplication.progress["retry_due_at"].as_string(),
                    "Z",
                    "+00:00",
                )
                retry_cutoff = TypeAdapter(datetime).dump_python(
                    _aware(now), mode="json"
                )
                statement = (
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
                            )
                        ),
                        or_(
                            retry_at.is_(None),
                            retry_at <= str(retry_cutoff).replace("Z", "+00:00"),
                        ),
                    )
                    .order_by(FleetProfileApplication.id)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
                cursor = self._recovery_cursor
                rows = list(
                    session.scalars(
                        statement
                        if cursor is None
                        else statement.where(FleetProfileApplication.id > cursor)
                    )
                )
                if (
                    cursor is not None
                    and len(rows) < _MAX_PARKED_APPLICATION_OBSERVATIONS
                ):
                    rows.extend(
                        session.scalars(
                            statement.where(FleetProfileApplication.id <= cursor).limit(
                                _MAX_PARKED_APPLICATION_OBSERVATIONS - len(rows)
                            )
                        )
                    )
                self._recovery_cursor = rows[-1].id if rows else None
                for row in rows:
                    try:
                        progress = _persisted_profile_progress(row)
                    except FleetProfileConflict:
                        continue
                    if not self._recovery_possible(session, row, progress):
                        continue
                    repeated = self._repeated_failure(session, row, progress)
                    if repeated is not None:
                        if not any(
                            item.code == PROFILE_REPEATED_FAILURE_CODE
                            for item in progress.blockers
                        ):
                            ended.append((row.id, repeated))
                        continue
                    if progress.retry_due_at is not None and _aware(
                        progress.retry_due_at
                    ) > _aware(now):
                        continue
                    if now < FleetProfileAdapter.next_retry(
                        row.id, progress.attempt, _aware(row.updated_at)
                    ):
                        continue
                    self._recovery_cursor = row.id
                    return row.id, row.actor
        finally:
            self._end_repeated_failures(ended, now)
        return None

    @staticmethod
    def _superseding_intent(
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Fence unissued profile effects after a newer authorized intent."""

        intended = progress.intended_profile
        if intended is None:
            return True
        if row.selection_generation is not None:
            if not FleetProfileService._application_is_current_selection(
                session, row, progress
            ):
                return True
        else:
            profile = session.get(FleetProfile, row.profile_id)
            if (
                profile is None
                or _digest(_profile_document(profile)) != intended.profile_digest
            ):
                return True
        plan = _persisted_profile_plan(row)
        if isinstance(plan, Residue):
            # Without its reviewed plan the order cannot show what it still owns:
            # it is retired as superseded (its issued effects keep their own
            # cancellation receipts).
            return True
        scope = {node_id for step in plan.steps for node_id in step.node_ids}
        if not scope:
            return False
        ordinal = progress.workload_intent_ordinal
        if ordinal is None:
            return True
        nodes = list(
            session.scalars(select(AgentNode).where(AgentNode.node_id.in_(scope)))
        )
        return len(nodes) != len(scope) or any(
            node.workload_intent_ordinal != ordinal for node in nodes
        )

    def _start_step(
        self,
        application_id: str,
        step_index: int,
        step: Mapping[str, object],
        *,
        actor: str,
    ) -> tuple[str | None, bool, FleetProfileChildOperation | None] | Residue:
        """Issue one plan step.  A :class:`Residue` means it cannot be issued now.

        The caller leaves the step where it is and looks again on its next pass:
        an executor that is not bound yet, or a child the executor cannot show,
        is unknown, not a failure of the load.
        """

        kind = step.get("kind")
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"vonk-forge:profile:{application_id}:{step_index}"
            )
        )
        if kind == "switch":
            if self._switch_adapter is None:
                return retire_as_unknown(
                    "profile-step",
                    application_id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "the Run/Switch executor is not bound yet",
                )
            execution_scope = tuple(
                read_stored_model(FleetProfilePlanStep, step).node_ids
            )
            if not execution_scope:
                # A switch that affects no Spark has no effect to issue: the step
                # is complete (a synchronous no-op).
                return None, True, None
            assignments = tuple(
                assignment
                for assignment in self._application_assignments(application_id)
                if {node.node_id for node in assignment.nodes} <= set(execution_scope)
            )
            child = self._switch_adapter.start(
                application_id=application_id,
                assignments=assignments,
                scope_node_ids=execution_scope,
                actor=actor,
                request_id=request_id,
            )
            if isinstance(child, Residue):
                return child
            if not isinstance(child, FleetProfileChildOperation):
                return retire_as_unknown(
                    "profile-step",
                    application_id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the Run/Switch executor returned no child operation",
                )
            return child.id, False, child
        raise FleetProfileInvalid(
            "Fleet profile step kind is unsupported",
            reason=InvalidRequestReason.UNSUPPORTED,
        )

    def _validated_assignments(
        self, session: Session, values: Sequence[FleetProfileAssignmentInput]
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Canonical assignments to store, and what saving replaced.

        A profile re-submits every assignment on each edit, so a choice the
        recipe stopped offering (a refreshed revision removed or renamed it)
        must not block saving the profile. It is replaced by the recipe
        default, and the notes name each replacement for the caller to show.
        """

        assignments: list[dict[str, object]] = []
        notes: list[str] = []
        for value in values:
            resolved = self._recipe_document(session, value.recipe_selector)
            if isinstance(resolved, Residue):
                # A save names its recipes: one without an active revision cannot
                # be chosen (a malformed request, refused at submit time).
                raise FleetProfileInvalid(
                    f"recipe has no active catalog revision: {value.recipe_selector}",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            document, revision = resolved
            # Every option is saved with an explicit value: the operator's
            # choice, or the recipe default where none was made or offered.
            choices, replaced = _effective_option_choices(
                revision.document, value.option_choices
            )
            notes.extend(f"{value.recipe_selector}: {note}" for note in replaced)
            # Save the canonical catalog selector.  This is a logical recipe
            # choice; its active revision is deliberately resolved later.
            normalized = value.model_copy(
                update={
                    "recipe_selector": self._recipe_selector(document),
                    "option_choices": choices,
                }
            )
            assignments.append(json.loads(canonical_message(normalized)))
        return assignments, notes

    @staticmethod
    def _reserve_saved_profile_references(
        session: Session,
        assignments: Sequence[FleetProfileAssignmentInput],
        *,
        now: datetime,
    ) -> None:
        """Serialize new durable selector refs with exact artifact removal."""

        model_sets: set[str] = set()
        runtime_images: set[str] = set()
        try:
            for assignment in assignments:
                resolved = FleetProfileService._recipe_document(
                    session, assignment.recipe_selector
                )
                if isinstance(resolved, Residue):
                    continue
                _, revision = resolved
                model_sets.update(
                    session.scalars(
                        select(ModelCacheSet.artifact_set_sha256).where(
                            ModelCacheSet.recipe_revision_sha256
                            == revision.content_digest
                        )
                    )
                )
                runtime_images.update(revision_archives(session, [revision.id]))
            if model_sets:
                require_model_sets_open(session, sorted(model_sets), now=now)
            if runtime_images:
                require_reference_open(
                    session,
                    (
                        ArtifactIdentity("runtime-image", digest)
                        for digest in sorted(runtime_images)
                    ),
                    now=now,
                )
        except ArtifactLifecycleError as error:
            raise FleetProfileUnavailable(
                f"{error.code}: {error.detail}",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error

    @staticmethod
    def _reserve_preview_assets(
        session: Session, preview: FleetProfilePreview, *, now: datetime
    ) -> None:
        """Gate every exact asset before accepting application effects."""

        model_sets = sorted(
            {item.model.artifact_set_sha256 for item in preview.preparation_decisions}
        )
        runtime_images = sorted(
            {
                item.runtime_image.oci_layout_sha256
                for item in preview.preparation_decisions
            }
        )
        try:
            if model_sets:
                require_model_sets_open(session, model_sets, now=now)
            if runtime_images:
                require_reference_open(
                    session,
                    (
                        ArtifactIdentity("runtime-image", digest)
                        for digest in runtime_images
                    ),
                    now=now,
                )
        except ArtifactLifecycleError as error:
            # The artifact owner holds the gate (a removal or an inspection is in
            # progress): admission waits, parked and retried by its owner, and the
            # load is planned again against what is there when the gate opens.
            raise FleetProfileAdmissionEffectBusy(
                f"{error.code}: {error.detail}"
            ) from error

    def _view(self, session: Session, row: FleetProfile) -> FleetProfileView:
        choices = self._choices(row)
        assignments: list[FleetProfileAssignmentView] = []
        cache_cached = 0
        cache_missing = 0
        cache_unknown = 0
        warnings: list[str] = []
        unreadable = self._unreadable_choice_count(row)
        if unreadable:
            warnings.append(
                f"{unreadable} saved choice(s) cannot be read and are left out; "
                "save the profile again"
            )
        selection = self._selected_profile_snapshot(session)
        if isinstance(selection, Residue):
            # Preserve draft reads even if the selected receipt is damaged.
            # The application worker owns failing that receipt explicitly.
            selection = None
            warnings.append("The selected profile application is unreadable")
        loaded_assignments = (
            selection.intended.assignments
            if selection is not None and selection.profile_id == row.id
            else None
        )
        assigned_nodes = (
            {
                node.node_id
                for assignment in loaded_assignments
                for node in assignment.nodes
            }
            if loaded_assignments is not None
            else {node_id for choice in choices for node_id in choice.spark_ids}
        )
        for choice in choices:
            resolved_head = self._recipe_document(session, choice.recipe_selector)
            head_error: str | None = None
            if isinstance(resolved_head, Residue):
                head_error = resolved_head.note
            else:
                try:
                    read_catalog_document(resolved_head[1])
                except ValueError as error:
                    head_error = str(error)
            if head_error is not None:
                # No readable (or no active) revision exists for this recipe (the
                # identity lookup already prefers the newest readable one). Show the
                # choice as needing attention instead of failing the profile.
                _LOGGER.warning(
                    "profile %s choice %s needs attention: %s",
                    row.id,
                    choice.recipe_selector,
                    head_error,
                )
                warnings.append(
                    f"Recipe {choice.recipe_selector} needs attention: "
                    "no readable revision is available; the recipe catalog "
                    "sync will replace it"
                )
                cache_unknown += 1
                assignments.append(
                    self._attention_assignment(
                        session, choice, loaded_assignments=loaded_assignments
                    )
                )
                continue
            resolved_choice = self._resolve_choice(session, choice)
            if isinstance(resolved_choice, Residue):
                # (Unreachable in practice: the head resolved just above.)
                cache_unknown += 1
                assignments.append(
                    self._attention_assignment(
                        session, choice, loaded_assignments=loaded_assignments
                    )
                )
                continue
            recipe, revision, cache = resolved_choice
            required = recipe_topology(revision.document).node_count
            if self._cache_resolver is None:
                warnings.append(
                    "Cache resolution is unavailable until the cache service is configured"
                )
            recipe_part = (
                require_mapping(
                    cache.get("recipe", {}), "profile cache recipe is invalid"
                )
                if cache
                else None
            )
            model_part = (
                require_mapping(
                    cache.get("model", {}), "profile cache model is invalid"
                )
                if cache
                else None
            )
            recipe_state = (
                "Cached"
                if recipe_part and bool(recipe_part.get("cached"))
                else "Recipe not cached"
            )
            cache_blockers = cache.get("blockers") if cache is not None else None
            if isinstance(cache_blockers, Sequence) and any(
                blocker == ModelCacheBlockerCode.RECIPE_NOT_CACHED
                for blocker in cache_blockers
            ):
                # The selected exact revision is bound into the profile, so the
                # operator has to prepare this cache entry rather than accept a
                # silently different older revision.
                warnings.append(
                    f"Recipe {recipe.publisher}/{recipe.slug} revision "
                    f"{revision.revision_number} is not in the local cache; "
                    "prepare the exact cache entry before applying"
                )
            model_state = (
                "Cached"
                if model_part and bool(model_part.get("cached"))
                else "Model not cached"
            )
            if cache is None:
                cache_unknown += 1
            elif recipe_state == "Cached" and model_state == "Cached":
                cache_cached += 1
            else:
                cache_missing += 1
            resources = (
                dict(
                    require_mapping(
                        cache.get("resources", {}),
                        "profile cache resources are invalid",
                    )
                )
                if cache
                else {}
            )
            model_document = sequence(
                resolve_recipe_entities(session, revision.document).get("models", ())
            )
            candidate_model = next(iter(model_document or ()), None)
            model = (
                candidate_model
                if isinstance(candidate_model, CatalogDocumentRevision)
                else None
            )
            model_selector = None
            model_name = None
            if model is not None:
                model_selector = f"{model.publisher}/{model.slug}"
                model_name = self._model_title(session, revision.document)
            assignment_selector = self._assignment_selector(choice)
            effective_choices, choice_notes = _effective_option_choices(
                revision.document, choice.option_choices
            )
            warnings.extend(
                f"{recipe.publisher}/{recipe.slug}: {note}" for note in choice_notes
            )
            recipe_update = self._loaded_recipe_update(
                session, loaded_assignments, recipe, revision, choice.spark_ids
            )
            assignments.append(
                FleetProfileAssignmentView(
                    selector=assignment_selector,
                    display_name=recipe.title,
                    recipe_selector=f"{recipe.publisher}/{recipe.slug}",
                    recipe_id=recipe.id,
                    spark_ids=list(choice.spark_ids),
                    required_sparks=required,
                    assigned_sparks=len(choice.spark_ids),
                    model={
                        "selector": model_selector,
                        "name": model_name,
                        "variant": choice.model_variant,
                        "state": model_state,
                        "content_sha256": (
                            model_part.get("content_sha256")
                            if model_part is not None
                            else None
                        ),
                    },
                    recipe={
                        "selector": f"{recipe.publisher}/{recipe.slug}",
                        "name": recipe.title,
                        "state": recipe_state,
                        "revision_id": revision.id,
                    },
                    resources=resources,
                    option_choices=effective_choices,
                    observed_state=self._observed_assignment_state(
                        session,
                        loaded_assignments,
                        recipe_id=recipe.id,
                        spark_ids=choice.spark_ids,
                    ),
                    recipe_update=recipe_update,
                )
            )
        roster = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.revoked_at.is_(None))
                .order_by(AgentNode.node_id)
            )
        )
        roster_node_ids = tuple(node.node_id for node in roster)
        roster_authority_blocked = bool(
            selection is not None
            and selection.profile_id == row.id
            and roster_node_ids != selection.roster_node_ids
            and not self._user_has_profile_authority(
                session.scalar(select(User).where(User.subject == selection.actor)),
                mutation=True,
            )
        )
        if roster_authority_blocked:
            warnings.append(
                "Fleet membership changed since this profile was loaded, but "
                "automatic reconciliation is blocked because the original "
                "profile author no longer has profile-load authority. Restore "
                "that authority or explicitly load this profile as an "
                "authorized administrator; roster reconciliation will retry "
                "automatically when authority is available."
            )
        display_names = {
            item.node_id: item.display_name
            for item in session.scalars(
                select(AgentNodeProfile).where(
                    AgentNodeProfile.node_id.in_([node.node_id for node in roster])
                )
            )
        }
        fleet: list[dict[str, object]] = [
            {
                "selector": node.node_id,
                "display_name": display_names.get(node.node_id, node.node_id),
                "state": "Assigned" if node.node_id in assigned_nodes else "Idle",
            }
            for node in roster
        ]
        document = _profile_document(row)
        loaded_revision = (
            selection.profile_revision
            if selection is not None and selection.profile_id == row.id
            else None
        )
        return FleetProfileView(
            id=row.id,
            number=row.number,
            revision=row.revision,
            name=row.name,
            description=row.description,
            installation_policy=_INSTALLATION_POLICY_ADAPTER.validate_python(
                row.installation_policy, strict=True
            ),
            labels=dict(row.labels),
            favorite=row.favorite,
            definition=self._definition(row),
            assignments=assignments,
            fleet=fleet,
            status="loaded" if loaded_revision is not None else "draft",
            loaded_revision=loaded_revision,
            cache_summary={
                "cached": cache_cached,
                "missing": cache_missing,
                "unknown": cache_unknown,
            },
            warnings=sorted(set(warnings)),
            next_actions=[
                *(
                    [
                        (
                            "Restore the original profile author's profile-load "
                            "authority or explicitly load this profile as an "
                            "authorized administrator."
                        )
                    ]
                    if roster_authority_blocked
                    else []
                ),
                f"vonkctl --profile {row.number} profile load",
            ],
            profile_digest=_digest(document),
            created_by=row.created_by,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _loaded_recipe_update(
        session: Session,
        loaded_assignments: Sequence[FleetProfileAssignment] | None,
        recipe: CatalogDocument,
        newest: CatalogDocumentRevision,
        spark_ids: Sequence[str],
    ) -> RecipeUpdateNotice | None:
        """Say when the loaded workload runs an older revision than `newest`.

        Load and run always resolve the newest revision, so this only ever
        describes what is already running; it never restarts anything.
        """

        if loaded_assignments is None:
            return None
        nodes = set(spark_ids)
        loaded = next(
            (
                assignment
                for assignment in loaded_assignments
                if assignment.recipe_id == recipe.id
                and {node.node_id for node in assignment.nodes} == nodes
            ),
            None,
        )
        if loaded is None or loaded.recipe_revision_id == newest.id:
            return None
        running = session.get(CatalogDocumentRevision, loaded.recipe_revision_id)
        if running is None:
            return None
        return recipe_update_notice(recipe.title, running, newest)

    @classmethod
    def _observed_assignment_state(
        cls,
        session: Session,
        loaded_assignments: Sequence[FleetProfileAssignment] | None,
        *,
        recipe_id: str,
        spark_ids: Sequence[str],
    ) -> str:
        """Project one saved assignment onto the live loaded application.

        Only the currently selected application's exact assignment says what
        is loaded; a saved edit that application does not contain is honestly
        "Not loaded".  The label comes from the same assignment-state predicate
        that planning and endpoint publication use, never a second opinion.
        """

        if loaded_assignments is None:
            return "Not loaded"
        nodes = set(spark_ids)
        loaded = next(
            (
                assignment
                for assignment in loaded_assignments
                if assignment.recipe_id == recipe_id
                and {node.node_id for node in assignment.nodes} == nodes
            ),
            None,
        )
        if loaded is None:
            return "Not loaded"
        return _OBSERVED_ASSIGNMENT_LABELS[
            cls._assignment_state(session, loaded).current_state
        ]

    def _attention_assignment(
        self,
        session: Session,
        choice: FleetProfileAssignmentInput,
        *,
        loaded_assignments: Sequence[FleetProfileAssignment] | None,
    ) -> FleetProfileAssignmentView:
        document_id = session.scalar(
            select(CatalogDocument.id).where(
                CatalogDocument.kind == "recipe",
                CatalogDocument.publisher == choice.recipe_selector.split("/")[0],
                CatalogDocument.slug == choice.recipe_selector.split("/")[-1],
            )
        )
        return FleetProfileAssignmentView(
            selector=self._assignment_selector(choice),
            display_name=choice.recipe_selector,
            recipe_selector=choice.recipe_selector,
            recipe_id=document_id,
            spark_ids=list(choice.spark_ids),
            assigned_sparks=len(choice.spark_ids),
            model={"variant": choice.model_variant, "state": "Needs attention"},
            recipe={
                "selector": choice.recipe_selector,
                "state": "Needs attention",
            },
            observed_state=(
                self._observed_assignment_state(
                    session,
                    loaded_assignments,
                    recipe_id=document_id,
                    spark_ids=choice.spark_ids,
                )
                if document_id is not None
                else "Not loaded"
            ),
        )

    @staticmethod
    def _model_title(session: Session, document: Mapping[str, object]) -> str | None:
        # Both call paths validate the same persisted recipe document through
        # ``recipe_topology`` before this projection runs, so a corrupt
        # document already raises there. The only resolution failure reachable
        # here is a referenced model revision that is not currently active,
        # for which a missing display title is deliberate.
        try:
            models = resolve_recipe_entities(session, document).get("models")
        except (KeyError, RuntimeError, TypeError, ValueError):
            return None
        if not isinstance(models, Sequence) or not models:
            return None
        model = models[0]
        root = session.get(CatalogDocument, model.document_id)
        return root.title if root is not None else f"{model.publisher}/{model.slug}"

    class _AssignmentState:
        current_state: FleetProfileAssignmentState
        mapping: ClusterMapping | None
        installation: RecipeInstallation | None
        run: RecipeRun | None
        build: RecipeBuild | None
        installation_ready: bool

        def __init__(
            self,
            *,
            current_state: FleetProfileAssignmentState,
            mapping: ClusterMapping | None,
            installation: RecipeInstallation | None,
            run: RecipeRun | None,
            build: RecipeBuild | None,
            installation_ready: bool = False,
        ) -> None:
            self.current_state = current_state
            self.mapping = mapping
            self.installation = installation
            self.run = run
            self.build = build
            self.installation_ready = installation_ready

    @classmethod
    def _assignment_state(
        cls,
        session: Session,
        assignment: FleetProfileAssignment,
        *,
        expected_image: RuntimeImageIdentity | None = None,
    ) -> _AssignmentState:
        build = (
            (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image is not None and expected_image.build_id is not None
                else None
            )
            if expected_image is not None
            else session.scalar(
                select(RecipeBuild)
                .where(
                    RecipeBuild.recipe_revision_id == assignment.recipe_revision_id,
                    RecipeBuild.state == "succeeded",
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
                .limit(1)
            )
        )
        mappings = tuple(
            session.scalars(
                select(ClusterMapping)
                .where(
                    ClusterMapping.recipe_revision_id == assignment.recipe_revision_id,
                    ClusterMapping.topology_name == assignment.topology_name,
                    ClusterMapping.state == "ready",
                )
                .order_by(ClusterMapping.updated_at.desc(), ClusterMapping.id.desc())
            )
        )
        expected = {
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in assignment.nodes
        }
        recipe_revision = session.get(
            CatalogDocumentRevision, assignment.recipe_revision_id
        )
        # An installation and run made with other option choices are not this
        # assignment's, even for the same recipe revision and Sparks.
        wanted_choices = (
            _effective_option_choices(
                recipe_revision.document, assignment.option_choices
            )[0]
            if recipe_revision is not None
            else dict(assignment.option_choices)
        )
        mapping = None
        for candidate in mappings:
            if mapping_option_choices(candidate.parameters) != wanted_choices:
                continue
            members = tuple(
                session.scalars(
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id == candidate.id
                    )
                )
            )
            actual = {
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in members
            }
            if actual == expected:
                mapping = candidate
                break
        if mapping is None:
            return cls._AssignmentState(
                current_state=ObservedAssignmentState.NOT_PLACED,
                mapping=None,
                installation=None,
                run=None,
                build=build,
            )
        installation = next(
            (
                candidate
                for candidate in session.scalars(
                    select(RecipeInstallation)
                    .where(
                        RecipeInstallation.mapping_id == mapping.id,
                        RecipeInstallation.recipe_revision_id
                        == assignment.recipe_revision_id,
                        RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES),
                    )
                    .order_by(
                        RecipeInstallation.updated_at.desc(),
                        RecipeInstallation.id.desc(),
                    )
                )
                # An installation compiled for a port the platform no longer
                # assigns cannot launch; the load installs the recipe again.
                if installation_serves_authorised_ports(candidate)
            ),
            None,
        )
        if installation is None:
            return cls._AssignmentState(
                current_state=ObservedAssignmentState.PLACED,
                mapping=mapping,
                installation=None,
                run=None,
                build=build,
            )
        install_members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                )
            )
        )
        exact_installed = (
            installation.state == InstallationState.INSTALLED
            and len(install_members) == len(expected)
            and {(node.node_id, node.rank, node.role) for node in install_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(
                node.state == InstallationNodeState.INSTALLED
                for node in install_members
            )
            and (
                installation_matches_runtime_image(
                    installation,
                    image_digest=expected_image.image_digest,
                    oci_layout_sha256=expected_image.oci_layout_sha256,
                    image_bytes=expected_image.image_bytes,
                )
                if expected_image is not None
                else (
                    build is None
                    or installation_matches_runtime_image(
                        installation,
                        image_digest=build.image_digest,
                        oci_layout_sha256=build.oci_layout_sha256,
                        image_bytes=build.image_bytes,
                    )
                )
            )
        )
        if not exact_installed:
            state: FleetProfileAssignmentState = (
                ObservedAssignmentState.INSTALLING
                if installation.state
                in {InstallationState.PLANNED, InstallationState.INSTALLING}
                else ObservedAssignmentState.DEGRADED
            )
            return cls._AssignmentState(
                current_state=state,
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
            )
        run = session.scalar(
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation.id,
                RecipeRun.state.in_(STOPPABLE_RUN_STATES),
            )
            .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
            .limit(1)
        )
        if run is None:
            return cls._AssignmentState(
                current_state=ObservedAssignmentState.INSTALLED,
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
                installation_ready=True,
            )
        run_members = tuple(
            session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        )
        live_run_node_ids = set(
            session.scalars(
                select(AgentNode.node_id).where(
                    AgentNode.node_id.in_(tuple(node.node_id for node in run_members)),
                    AgentNode.revoked_at.is_(None),
                )
            )
        )
        healthy = (
            run.state == RunState.RUNNING
            and run.route_state == RouteState.PUBLISHED
            and len(run_members) == len(expected)
            and live_run_node_ids == {node.node_id for node in run_members}
            and {(node.node_id, node.rank, node.role) for node in run_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(node.state == RunState.RUNNING for node in run_members)
        )
        return cls._AssignmentState(
            current_state=(
                ObservedAssignmentState.RUNNING
                if healthy
                else ObservedAssignmentState.DEGRADED
            ),
            mapping=mapping,
            installation=installation,
            run=run,
            build=build,
            installation_ready=True,
        )

    @staticmethod
    def _installation_nodes(
        session: Session, installation_ids: Sequence[str]
    ) -> dict[str, tuple[InstallationNode, ...]]:
        grouped: dict[str, list[InstallationNode]] = {}
        if installation_ids:
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id.in_(installation_ids)
                )
            ):
                grouped.setdefault(node.installation_id, []).append(node)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _run_nodes(
        session: Session, run_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        if run_ids:
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id.in_(run_ids))
            ):
                grouped.setdefault(node.run_id, []).append(node.node_id)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _installation_node_ids(
        session: Session, installation_id: str
    ) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )

    def _application_view(
        self, row: FleetProfileApplication
    ) -> FleetProfileApplicationView:
        # Damaged documents degrade the view, they do not refuse it: a plan that
        # cannot be read shows the step count the progress recorded and no
        # cancellation projection; damaged progress and result are rebuilt from the
        # receipt the row itself carries.
        plan = _persisted_profile_plan(row)
        progress = _persisted_profile_progress(row)
        if (
            row.state in {"queued", "running"}
            and progress.child_progress
            and progress.child_progress.operation
        ):
            progress.child_progress.operation = project_progress(
                progress.child_progress.operation, _aware(self._clock())
            )
        state, next_attempt_at = self._presented_state(row, progress)
        return FleetProfileApplicationView(
            id=row.id,
            request_key=row.request_key,
            profile_id=row.profile_id,
            profile_digest=row.profile_digest,
            plan_digest=row.plan_digest,
            state=state,
            attempt=progress.attempt,
            retry_of_application_id=progress.retry_of_application_id,
            superseded_by=progress.superseded_by if state == "superseded" else None,
            reason_code=progress.supersede_code if state == "superseded" else None,
            current_step=row.current_step,
            total_steps=progress.total_steps
            if isinstance(plan, Residue)
            else len(plan.steps),
            current_operation_id=row.current_operation_id,
            status_reason=row.status_reason,
            progress=progress,
            cancellation=(
                None
                if isinstance(plan, Residue)
                else _application_cancellation_view(row, plan, progress)
            ),
            result=_persisted_profile_result(row),
            blockers=(
                list(progress.blockers)
                if state
                in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.FAILED,
                    LifecycleState.NEEDS_OPERATOR,
                )
                else []
            ),
            next_attempt_at=next_attempt_at,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _intended_profile(
        application: FleetProfileApplication,
        *,
        session: Session,
    ) -> FleetProfileIntendedConfiguration | Residue:
        """The accepted intent of an application, or why none can be established.

        An application whose accepted intent cannot be read (never recorded, or
        its reviewed plan is damaged) is a :class:`Residue`: the caller retires or
        skips it.  A digest that is *inconsistent* is a different matter and still
        refuses: it is the integrity check of what was reviewed.
        """

        progress = _stored_progress(application)
        if isinstance(progress, Residue):
            return progress
        if progress.intended_profile is None:
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.ROW_INCOMPLETE,
                "the application carries no accepted intent",
            )
        if progress.intended_profile.profile_digest != application.profile_digest:
            raise FleetProfileUnavailable(
                "Persisted application intent digest is inconsistent",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        intended = progress.intended_profile
        root = (
            application
            if intended.reviewed_application_id == application.id
            else session.get(FleetProfileApplication, intended.reviewed_application_id)
        )
        if (
            root is None
            or root.profile_id != application.profile_id
            or root.profile_digest != application.profile_digest
        ):
            raise FleetProfileUnavailable(
                "Persisted application review source is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        root_progress = _stored_progress(root)
        if isinstance(root_progress, Residue):
            return root_progress
        root_plan = _persisted_profile_plan(root)
        if isinstance(root_plan, Residue):
            return root_plan
        if (
            root_progress.retry_of_application_id is not None
            or root_progress.intended_profile != intended
            or intended.reviewed_application_id != root.id
            or intended.reviewed_plan_digest != _digest(root_plan.reviewed_decision())
        ):
            raise FleetProfileUnavailable(
                "Persisted application review digest is inconsistent",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        plan = _persisted_profile_plan(application)
        if isinstance(plan, Residue):
            return plan
        if (
            plan.scope.node_ids != intended.scope.node_ids
            or plan.resolved_assignments
            != sorted(intended.assignments, key=lambda item: item.id)
        ):
            raise FleetProfileInvalid(
                "Persisted application plan exceeds its reviewed intent",
                reason=InvalidRequestReason.CONFLICT,
            )
        _validate_remaining_effects(root_plan.effects, plan.effects)
        return intended

    @staticmethod
    def _reviewed_profile_plan(
        application: FleetProfileApplication, *, session: Session
    ) -> FleetProfilePreview | Residue:
        intended = FleetProfileService._intended_profile(application, session=session)
        if isinstance(intended, Residue):
            return intended
        reviewed = session.get(
            FleetProfileApplication, intended.reviewed_application_id
        )
        if reviewed is None:
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the reviewed application is not stored",
            )
        return _persisted_profile_plan(reviewed)

    def _application_assignments(
        self, application_id: str
    ) -> tuple[FleetProfileAssignment, ...]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            intended = self._intended_profile(application, session=session)
            if isinstance(intended, Residue):
                return ()
            return tuple(sorted(intended.assignments, key=lambda item: item.id))


__all__ = [
    "RETRY_SUPERSEDE",
    "RETRY_WAIT",
    "FleetProfileAdmissionBusy",
    "FleetProfileAdmissionEffectBusy",
    "FleetProfileConflict",
    "FleetProfileResourceRecheckUnavailable",
    "FleetProfileReviewStale",
    "FleetProfileSelectionLost",
    "FleetProfileService",
    "FleetProfileStalePlanConflict",
    "RunSwitchFleetProfileAdapter",
    "retry_disposition_of",
]
