"""PostgreSQL authority for saved Fleet profiles and live-versus-desired plans."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, TypedDict

from pydantic import ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import String, case, cast, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

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
from .fleet_profile_contract import (
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
    FleetProfileSwitchAdapter,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileView,
    profile_switch_child_request_key,
)
from .logging import redact_text
from .models import (
    AgentNode,
    AgentNodeProfile,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
    RuntimeImageAuthorization,
    User,
)
from .operation_contract import OperationFailureEvidence
from .operation_progress import project_progress
from .preparation_contract import RolloutPreparation, RuntimeImageIdentity
from .profile_capacity import (
    release_unassigned_profile_claims,
    reserve_profile_disk,
    reserve_profile_memory,
    reserve_profile_ports,
)
from .recipe_build_cancellation import (
    BuildConsumerError,
    lock_profile_build_dependencies,
)
from .recipe_execution_contract import installation_matches_runtime_image
from .recipe_operations import RecipeOperationConflict
from .recipe_runtime_specs import (
    recipe_topology,
    resolve_recipe_entities,
)
from .run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchAssessment,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPlacementAction,
    RunSwitchPlan,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from .run_switch_operations import (
    RunSwitchOperationConflict,
    RunSwitchOperationService,
)
from .strict_json import stored_document_detail
from .user_authority import serialize_user_authority

if TYPE_CHECKING:
    from .operation_api import OperationProviderProtocol

_STORED_ASSIGNMENTS = TypeAdapter(
    list[FleetProfileAssignmentInput], config=ConfigDict(strict=True)
)
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_ACTIVE_RUN_STATES = frozenset({"planned", "starting", "running", "stopping"})
_ACTIVE_INSTALL_STATES = frozenset(
    {"planned", "installing", "installed", "partial", "failed"}
)
_CHILD_PENDING_STATES = frozenset(
    {"queued", "pending", "leased", "running", "starting", "stopping", "installing"}
)
_CHILD_FAILED_STATES = frozenset(
    {"failed", "expired", "cancelled", "waiting-for-operator"}
)
_PROFILE_ACTIVITY_ACTIVE_STATES = ("queued", "running", "waiting-for-operator")
# Decoded and database-sourced closed values are read back through the
# contract's own alias, so a malformed state fails instead of reaching a typed
# model as an unvalidated string.
_OPERATION_STATE_ADAPTER = TypeAdapter(FleetProfileOperationState)
_PROFILE_PHASE_ADAPTER = TypeAdapter(FleetProfileChildPhase)
_INSTALLATION_POLICY_ADAPTER = TypeAdapter(FleetProfileInstallationPolicy)
_MAX_CACHE_RECOVERY_DELAY_SECONDS = 60
_MAX_ADMISSION_RETRY_DELAY_SECONDS = 60
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


def _profile_activity_pending(state, cancellation_state):
    """One pending predicate for in-memory display and SQL selection."""

    active = (
        state in _PROFILE_ACTIVITY_ACTIVE_STATES
        if isinstance(state, str)
        else state.in_(_PROFILE_ACTIVITY_ACTIVE_STATES)
    )
    return active & (cancellation_state == "cancelling")


def _profile_activity_state(
    state: str, cancellation: FleetProfileApplicationCancellationIntent | None
) -> str:
    """Project an active application from its durable cancellation owner."""

    if _profile_activity_pending(
        state, cancellation.state if cancellation is not None else None
    ):
        return "cancelling"
    return state


def _profile_activity_state_expression():
    return case(
        (
            _profile_activity_pending(
                FleetProfileApplication.state,
                FleetProfileApplication.progress["cancellation"]["state"].as_string(),
            ),
            "cancelling",
        ),
        else_=FleetProfileApplication.state,
    )


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
    ) -> RunSwitchAssessment: ...


@dataclass(frozen=True)
class _ProfileControlEffects:
    """One SQL-owned reconciliation projection for review and admission."""

    states: dict[str, FleetProfileService._AssignmentState]
    effects: FleetProfileEffects
    changed_nodes: set[str]
    switch_needed: bool
    reasons: list[FleetProfileReason]


class _PlanStepDraftRequired(TypedDict):
    """Required keys of one plan step before its index is assigned."""

    kind: FleetProfilePlanStepKind
    label: str


class _PlanStepDraft(_PlanStepDraftRequired, total=False):
    """Optional keys of one plan step before its index is assigned."""

    node_ids: list[str]


def _persisted_profile_plan(row: FleetProfileApplication) -> FleetProfilePreview:
    """Load the complete stored preview through its canonical contract."""

    try:
        plan = FleetProfilePreview.model_validate_json(
            json.dumps(row.plan), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise FleetProfileConflict("Persisted Fleet profile plan is invalid") from error
    if (
        plan.profile_id != row.profile_id
        or plan.profile_digest != row.profile_digest
        or plan.plan_digest != row.plan_digest
    ):
        raise FleetProfileConflict("Persisted Fleet profile plan identity is invalid")
    return plan


def _profile_application_effect_nodes(plan: FleetProfilePreview) -> tuple[str, ...]:
    """Return the exact sorted node scope of reviewed profile effects."""

    return tuple(sorted({node_id for step in plan.steps for node_id in step.node_ids}))


def _persisted_profile_result(
    row: FleetProfileApplication,
) -> FleetProfileApplicationResult | None:
    """Load a stored result without treating malformed JSON as no result."""

    if row.result is None:
        if row.state == "succeeded":
            raise FleetProfileConflict("Persisted Fleet profile result is invalid")
        return None
    try:
        return FleetProfileApplicationResult.model_validate_json(
            json.dumps(row.result), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise FleetProfileConflict(
            "Persisted Fleet profile result is invalid"
        ) from error


def _canonical_progress(value: object) -> FleetProfileApplicationProgress:
    """Validate a persisted progress document with canonical JSON semantics.

    The document was written as JSON: nested contract tuples arrive as arrays
    and unions must resolve the way they did on the producer side.  Validating
    an already-decoded mapping as strict Python data instead lets Pydantic's
    smart union pick a different variant, which silently replaces a child
    receipt with a same-shaped neighbour.
    """

    return FleetProfileApplicationProgress.model_validate_json(
        canonical_message(value), strict=True
    )


def _persisted_profile_progress(
    row: FleetProfileApplication,
) -> FleetProfileApplicationProgress:
    """Load progress through its canonical contract before worker mutation."""

    try:
        return FleetProfileApplicationProgress.model_validate_json(
            canonical_message(row.progress), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise FleetProfileConflict(
            "Persisted Fleet profile progress is invalid"
        ) from error


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


def _stored_retry_lineage(value: object) -> str | None:
    """Read only the explicit retry lineage one stored receipt declared.

    Retry authority is the durable workload-intent ordinal on each node plus
    this explicit lineage.  A damaged historical row contributes no lineage
    instead of forcing the projection to decode every sibling document.
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


class FleetProfileConflict(RuntimeError):
    """A Fleet profile is invalid, stale, or cannot be safely applied."""


class FleetProfileAdmissionBusy(FleetProfileConflict):
    """A transient admission owner must finish before the plan can be bound."""

    code = "profile.admission_busy"


class FleetProfileAdmissionEffectBusy(FleetProfileConflict):
    """A live effect owner must finish before a superseding plan can bind."""

    code = "profile.admission_effect_busy"


class FleetProfileStalePlanConflict(FleetProfileConflict):
    """Admission refused because the caller's reviewed plan is no longer current."""

    code = "profile.stale_plan"


class FleetProfilePermissionDenied(PermissionError):
    """Current user authority cannot authorize this profile request."""


class _FleetProfileRecoveryBindingConflict(FleetProfileConflict):
    """Recovery cannot adopt the currently available artifact identity."""


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
) -> None:
    if expected is None:
        raise _FleetProfileRecoveryBindingConflict(
            "profile.recovery_identity_unavailable: The accepted artifact identity "
            f"for assignment {assignment_id} is unavailable; an explicit new load "
            "must bind verified cache assets."
        )
    image = expected.runtime_image
    if observed is None:
        raise _FleetProfileRecoveryBindingConflict(
            "profile.recovery_cache_pending: Prepare cache on the Controller for "
            f"assignment {assignment_id}, image {image.image_digest}, archive "
            f"{image.oci_layout_sha256}; recovery retains these exact identities."
        )
    if _recovery_preparation_identity(expected) != _recovery_preparation_identity(
        observed
    ):
        raise _FleetProfileRecoveryBindingConflict(
            "profile.recovery_artifact_changed: Prepared assets for assignment "
            f"{assignment_id} differ from its accepted model/image identity; "
            "restore the exact accepted assets or use an explicit new load "
            "to bind the replacement."
        )


def _cache_recovery_delay(attempt: int) -> timedelta:
    # Bound exponent work as well as the retry rate for long-lived intent.
    exponent = min(attempt - 1, _MAX_CACHE_RECOVERY_DELAY_SECONDS.bit_length())
    return timedelta(seconds=min(_MAX_CACHE_RECOVERY_DELAY_SECONDS, 2**exponent))


def _admission_retry_delay(attempt: int) -> timedelta:
    """Bound parked admission retries while preserving the latest intent."""

    exponent = min(attempt - 1, _MAX_ADMISSION_RETRY_DELAY_SECONDS.bit_length())
    return timedelta(seconds=min(_MAX_ADMISSION_RETRY_DELAY_SECONDS, 2**exponent))


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
        )


def _operation_state(
    value: object, *, default: FleetProfileOperationState
) -> FleetProfileOperationState:
    """Read one stored child operation state, keeping absence distinct.

    ``None`` is genuinely absent, so the caller's deliberate default stands.
    Any other value is a stored state that must satisfy the closed contract;
    inventing a state for a malformed one would hide corruption.
    """

    if value is None:
        return default
    try:
        return _OPERATION_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise FleetProfileConflict(
            "persisted profile child operation state is invalid"
        ) from error


def _string_items(value: object, detail: str) -> list[str]:
    """Read a decoded JSON string array, or raise when it is not exactly that."""

    items = sequence(value)
    if items is None:
        raise FleetProfileConflict(detail)
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise FleetProfileConflict(detail)
        result.append(item)
    return result


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
                raise KeyError(application_id)
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

    def recoverable_cache_loss(self, application_id: str, *, session: Session) -> bool:
        """Recognize only a typed, pre-effect Controller cache loss."""

        application = session.get(FleetProfileApplication, application_id)
        if application is None:
            return False
        state = self._state(application)
        if state is None or state.get("state") != "failed":
            return False
        active = state.get("active_operation_id")
        if not isinstance(active, str):
            return False
        try:
            child = self._run_switch.get(active)
        except (KeyError, RuntimeError, TypeError, ValueError):
            return False
        result = child.result
        return bool(
            child.state == "failed"
            and result is not None
            and result.phase == "prepare"
            and result.subphase == "runtime-image"
            and result.child_operation_id is None
            and result.failure_code == "runtime_image.cache_missing"
        )

    def start(
        self,
        *,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation:
        ordered_assignments = tuple(sorted(assignments, key=lambda item: item.id))
        if tuple(scope_node_ids) != tuple(sorted(set(scope_node_ids))):
            raise FleetProfileConflict(
                "profile switch scope must be the complete sorted node boundary"
            )
        scope = set(scope_node_ids)
        if any(
            node.node_id not in scope
            for assignment in ordered_assignments
            for node in assignment.nodes
        ):
            raise FleetProfileConflict(
                "profile switch assignment contains a node outside its bound scope"
            )
        with self._sessions.begin() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            existing = self._state(application)
            if (
                existing is None
                and _persisted_profile_progress(application).cancellation is not None
            ):
                raise FleetProfileConflict(
                    "Profile cancellation prevents dispatch of another child"
                )
            if existing is not None:
                expected_scope = list(scope_node_ids)
                expected_assignment_ids = [item.id for item in ordered_assignments]
                if (
                    existing.get("scope_node_ids") != expected_scope
                    or existing.get("assignment_ids") != expected_assignment_ids
                ):
                    raise FleetProfileConflict(
                        "profile switch child was replayed with a different "
                        "bound scope or assignments"
                    )
                return self._view_from_state(application, existing)
            intended = FleetProfileService._intended_profile(
                application, session=session
            )
            queue = self._plan_queue(
                session,
                ordered_assignments,
                scope_node_ids,
                installation_policy=intended.installation_policy,
                reviewed_effects=_persisted_profile_plan(application).effects,
                expected_images={
                    item.assignment_id: item.runtime_image
                    for item in FleetProfileService._reviewed_profile_plan(
                        application, session=session
                    ).preparation_decisions
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
                raise KeyError(operation_id)
            state = self._state(application)
            if state is None:
                raise KeyError(operation_id)
            active = state.get("active_operation_id")
            if isinstance(active, str):
                try:
                    child = self._run_switch.get(active)
                except KeyError as error:
                    raise RuntimeError("Run/Switch child is unavailable") from error
                return self._view_from_child(operation_id, state, child)
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
                raise KeyError(operation_id)
            state = self._state(application)
            if state is None:
                raise KeyError(operation_id)
            return self._advance(
                operation_id,
                self._assignments_from_state(state),
                session=session,
            )
        with self._sessions() as own:
            application = own.get(FleetProfileApplication, operation_id)
            if application is None:
                raise KeyError(operation_id)
            state = self._state(application)
            if state is None:
                raise KeyError(operation_id)
            assignments = self._assignments_from_state(state)
        return self._advance(operation_id, assignments)

    @staticmethod
    def _assignment_intent(
        assignment: FleetProfileAssignment,
    ) -> tuple[RunSwitchPlacementAction, str]:
        return (
            "install" if assignment.desired_state == "installed" else "switch",
            assignment.alias or assignment.recipe_title.lower().replace(" ", "-"),
        )

    @staticmethod
    def _assignment_request(
        session: Session,
        assignment: FleetProfileAssignment,
        *,
        request_key: str | None = None,
    ) -> RunSwitchApplyRequest:
        """Map one profile assignment onto the Run/Switch authority.

        This is the single definition of how a profile assignment becomes a
        Run/Switch request.  Both apply and preparation projection use it, so a
        preview can never describe a different model, revision, group or alias
        than the child operation it later binds.
        """

        revision = session.get(CatalogDocumentRevision, assignment.recipe_revision_id)
        if revision is None:
            raise KeyError(assignment.recipe_revision_id)
        resolved = resolve_recipe_entities(session, revision.document)
        models = resolved.get("models")
        model_digest = (
            models[0].content_digest
            if isinstance(models, Sequence) and models
            else None
        )
        if not isinstance(model_digest, str):
            raise FleetProfileConflict("Profile assignment has no exact model identity")
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
    ) -> RunSwitchAssessment:
        """Project admission and preparation from the same workload planner.

        Run/Switch remains the authority for the exact model artifact set, the
        runtime image identity and per-target readiness.  Asking it to plan is
        what keeps a profile preview honest: the preview shows the same
        preparation evidence the child operation will require, instead of a
        second, weaker estimate maintained at the profile boundary.
        """

        observed = tuple(sorted(node.node_id for node in assignment.nodes))
        if observed != expected_nodes:
            raise ValueError(
                "Profile assignment nodes changed during preparation projection."
            )
        request = self._assignment_request(session, assignment)
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
        assessments = {item.assignment_id: item for item in reviewed.assessments}
        decisions = {item.assignment_id: item for item in reviewed.admission_decisions}
        changing = {
            item.assignment_id
            for item in reviewed.assignments
            if item.current_state != item.desired_state
        }
        for assignment in assignments:
            if assignment.id not in changing:
                continue
            previous = assessments.get(assignment.id)
            decision = decisions.get(assignment.id)
            if previous is None or decision is None:
                raise FleetProfileConflict(
                    "Profile resource admission evidence is unavailable"
                )
            try:
                fresh = self._run_switch.recheck_resources_in_session(
                    session,
                    self._assignment_request(session, assignment),
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
                raise FleetProfileConflict(
                    "Profile resource admission is unavailable; review again"
                ) from error
            current = FleetProfileAdmissionDecision.from_assessment(
                FleetProfileAssignmentAssessment(
                    assignment_id=assignment.id, assessment=fresh
                )
            )
            if not current.allowed or current != decision:
                reasons = ", ".join(reason.code for reason in current.blockers[:4])
                raise FleetProfileStalePlanConflict(
                    "Profile resource admission changed; review again"
                    + (f": {reasons}" if reasons else "")
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
            raise KeyError(application_id)
        state = self._state(application)
        if state is None:
            raise KeyError(application_id)
        cancelling = _persisted_profile_progress(application).cancellation is not None
        active = state.get("active_operation_id")
        position = state.get("position")
        if isinstance(active, str):
            try:
                child = self._run_switch.get(active)
            except KeyError as error:
                raise RuntimeError("Run/Switch child is unavailable") from error
            if child.state in {"queued", "running"}:
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
                    state["state"] = view.state
                    state["child_progress"] = new_progress
                    self._write_state(session, application, state)
                    session.flush()
                return view
            if child.state == "waiting-for-operator":
                view = self._view_from_child(application_id, state, child)
                state["state"] = "waiting-for-operator"
                state["status_reason"] = child.status_reason
                state["child_progress"] = (
                    view.progress.model_dump(mode="json")
                    if view.progress is not None
                    else None
                )
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
                            "kind": state.get("active_kind"),
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
                reason = child.status_reason or (
                    f"Run/Switch child ended in {child.state}"
                )
                return self._failed_in_session(session, application, state, reason)
            if child.state != "succeeded":
                if cancelling:
                    state["state"] = "waiting-for-operator"
                    state["status_reason"] = (
                        f"Cancellation cannot reconcile Run/Switch child state "
                        f"{child.state}"
                    )[:512]
                    self._write_state(session, application, state)
                    session.flush()
                    return self._view_from_state(application, state)
                return self._failed_in_session(
                    session,
                    application,
                    state,
                    f"Run/Switch child returned {child.state}",
                )
            children = list(sequence(state.get("children")) or ())
            # Record the child's public result tree with the child.  A profile
            # step receipt is read back from this mirror, so a completion that
            # only updated progress would lose the Run/Switch receipt on the
            # very tick the child finishes, and again after any restart.
            receipt = self._child_receipt(child)
            children.append(
                {
                    "operation_id": child.operation_id,
                    "kind": state.get("active_kind"),
                    "state": child.state,
                    "result": (
                        receipt.model_dump(mode="json") if receipt is not None else None
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
            state["state"] = "succeeded"
            state["result"] = {
                "children": list(sequence(state.get("children")) or ()),
                "assignment_ids": list(sequence(state.get("assignment_ids")) or ()),
            }
            self._write_state(session, application, state)
            session.flush()
            return self._view_from_state(application, state)
        item = queue[integer(position) or 0]
        if not isinstance(item, Mapping):
            return self._failed_in_session(
                session,
                application,
                state,
                "Persisted profile switch queue is invalid",
            )
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
            _canonical_progress(application.progress).workload_intent_ordinal,
        )
        state["active_operation_id"] = operation.operation_id
        state["active_kind"] = item.get("kind")
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_child(application_id, state, operation)

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
        ordinal = _canonical_progress(application.progress).workload_intent_ordinal
        cancellation = _canonical_progress(application.progress).cancellation
        if cancellation is not None:
            ordinal = cancellation.workload_intent_ordinal
        if ordinal is None:
            raise FleetProfileConflict("Profile switch workload intent is unbound")
        scope_node_ids = _string_items(
            state.get("scope_node_ids", []),
            "profile switch child scope node IDs are invalid",
        )
        effects = AgentJobService.assess_superseded_agent_effects_in_session(
            session, scope_node_ids, ordinal, now
        )
        if not effects:
            state["observation_due_at"] = None
            state["observation_deadline_at"] = None
            state["pending_operation_ids"] = []
            state["status_reason"] = None
            return None
        deadline = min(effect.observation_deadline for effect in effects)
        due = min(effect.observe_due_at for effect in effects)
        operation_ids = sorted(effect.operation_id for effect in effects)
        next_state = "waiting-for-operator" if now >= deadline else "running"
        reason = (
            "An older issued workload has no definitive cancellation receipt: "
            if now >= deadline
            else "Waiting for older issued workload cancellation receipts: "
        ) + ", ".join(operation_ids)
        updated = {
            "state": next_state,
            "status_reason": reason[:512],
            "observation_due_at": None if now >= deadline else due.isoformat(),
            "observation_deadline_at": deadline.isoformat(),
            "pending_operation_ids": operation_ids,
        }
        if any(state.get(key) != value for key, value in updated.items()):
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
        state["state"] = "cancelled"
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
            progress_data["cancellation"]["state"] = "cancelled"
            application.progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(progress_data), strict=True
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
    ) -> RunSwitchOperation:
        if workload_intent_ordinal is None:
            raise FleetProfileConflict("Profile workload intent is unbound")
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
        with self._sessions() as session:
            reviewed = self._child_review(session, application_id)
        if kind == "cleanup":
            installation_id = item.get("id")
            if not isinstance(installation_id, str):
                raise FleetProfileConflict(
                    "Profile switch cleanup item has no installation identity"
                )
            cleanup_preview = self._run_switch.preview_cleanup(
                RunSwitchCleanupPreviewRequest(installation_id=installation_id),
                actor=actor,
            )
            self._validate_child_effects(reviewed, cleanup_preview)
            return self._run_switch.apply_cleanup(
                RunSwitchCleanupApplyRequest(
                    installation_id=installation_id,
                    plan_digest=cleanup_preview.plan_digest,
                    request_key=child_request_key,
                ),
                actor=actor,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        if kind == "stop":
            run_id = item.get("id")
            if not isinstance(run_id, str):
                raise FleetProfileConflict(
                    "Profile switch stop item has no run identity"
                )
            preview = self._run_switch.preview_stop(
                RunSwitchStopPreviewRequest(run_id=run_id), actor=actor
            )
            self._validate_child_effects(reviewed, preview)
            return self._run_switch.apply_stop(
                RunSwitchStopApplyRequest(
                    run_id=run_id,
                    plan_digest=preview.plan_digest,
                    request_key=child_request_key,
                ),
                actor=actor,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        assignment_id = item.get("id")
        assignment = next(
            (value for value in assignments if value.id == assignment_id), None
        )
        if assignment is None:
            raise FleetProfileConflict("Profile switch assignment is unavailable")
        with self._sessions() as session:
            request = self._assignment_request(
                session, assignment, request_key=child_request_key
            )
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            accepted = FleetProfileService._reviewed_profile_plan(
                application, session=session
            )
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
                assignment.id, expected_preparation, plan.preparation
            )
        if not plan.allowed:
            raise RunSwitchOperationConflict(
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
    def _child_review(session: Session, application_id: str) -> FleetProfilePreview:
        application = session.get(FleetProfileApplication, application_id)
        if application is None:
            raise KeyError(application_id)
        FleetProfileService._intended_profile(application, session=session)
        return _persisted_profile_plan(application)

    @staticmethod
    def _validate_child_effects(
        reviewed: FleetProfilePreview, child: RunSwitchPlan
    ) -> None:
        """A fresh child plan cannot enlarge the accepted parent's consent."""
        execution_nodes = {node for step in reviewed.steps for node in step.node_ids}
        if not {node.node_id for node in child.spark_group.nodes} <= execution_nodes:
            raise FleetProfileConflict("Profile child exceeds its reviewed Spark scope")
        stops = {
            effect.run_id: effect
            for effect in reviewed.effects.runs
            if effect.action == "stop"
        }
        for stop in child.stops:
            expected = stops.get(stop.run_id)
            if (
                expected is None
                or expected.alias != stop.alias
                or sorted(expected.node_ids) != sorted(stop.node_ids)
            ):
                raise FleetProfileConflict(
                    "Profile child would stop an unreviewed workload; review again"
                )
        if child.action == "cleanup" and not any(
            effect.action == "remove"
            and effect.installation_id == child.installation_id
            and sorted(effect.node_ids)
            == sorted(node.node_id for node in child.spark_group.nodes)
            for effect in reviewed.effects.installations
        ):
            raise FleetProfileConflict(
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
        workload_intent_ordinal: int,
    ) -> RunSwitchOperation | None:
        """Read the exact committed child before any mutable preview is redone."""

        with self._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_key))
            if job is None:
                return None
            kind = item.get("kind")
            if not isinstance(kind, str):
                raise FleetProfileConflict("Profile child kind is invalid")
            expected_kind = {
                "cleanup": "recipe.cleanup.v2",
                "stop": "recipe.stop.v2",
                "run": "recipe.run-switch.v2",
                "install": "recipe.run-switch.v2",
            }.get(kind)
            if job.kind != expected_kind:
                raise FleetProfileConflict(
                    "Profile child request key belongs to another operation"
                )
            if job.payload.get("workload_intent_ordinal") != workload_intent_ordinal:
                raise FleetProfileConflict(
                    "Profile child changed its bound workload intent"
                )
            raw_plan = job.payload.get("plan")
            try:
                plan = RunSwitchPlan.model_validate_json(
                    canonical_message(raw_plan), strict=True
                )
            except (TypeError, ValueError) as error:
                raise FleetProfileConflict(
                    "Persisted Run/Switch child plan is invalid"
                ) from error
            child_nodes = tuple(sorted(node.node_id for node in plan.spark_group.nodes))
            if child_nodes != tuple(sorted(job.targets)) or not set(child_nodes) <= set(
                scope_node_ids
            ):
                raise FleetProfileConflict(
                    "Profile child changed its bound Spark scope"
                )
            owner_id = item.get("id")
            if kind == "cleanup":
                valid = plan.action == "cleanup" and plan.installation_id == owner_id
            elif kind == "stop":
                valid = plan.action == "stop" and plan.run_id == owner_id
            else:
                receipt = RunSwitchOperationResult.model_validate_json(
                    canonical_message(job.result), strict=True
                )
                if receipt.profile_application_id != application_id:
                    raise FleetProfileConflict(
                        "Profile child changed its capacity owner"
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
                    == ("install" if assignment.desired_state == "installed" else "run")
                    and (plan.action, plan.alias) == expected
                    and plan.recipe_revision_id == assignment.recipe_revision_id
                    and [node.model_dump() for node in plan.spark_group.nodes]
                    == [
                        node.model_dump()
                        for node in sorted(assignment.nodes, key=lambda node: node.rank)
                    ]
                )
            if not valid:
                raise FleetProfileConflict(
                    "Profile child changed its bound owner or assignment"
                )
            self._validate_child_effects(
                self._child_review(session, application_id), plan
            )
            operation_id = job.id
        return self._run_switch.get(operation_id)

    def _plan_queue(
        self,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        *,
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
        )
        if any(reason.severity == "error" for reason in control.reasons):
            raise FleetProfileConflict(
                "Profile workload effects can no longer be represented safely; review again"
            )
        _validate_remaining_effects(reviewed_effects, control.effects)
        return [
            *(
                {"kind": "stop", "id": effect.run_id}
                for effect in control.effects.runs
                if effect.action == "stop"
            ),
            *(
                {
                    "kind": "install"
                    if assignment.desired_state == "installed"
                    else "run",
                    "id": assignment.id,
                }
                for assignment in assignments
                if control.states[assignment.id].current_state
                != assignment.desired_state
            ),
            *(
                {"kind": "cleanup", "id": effect.installation_id}
                for effect in control.effects.installations
                if effect.action == "remove"
            ),
        ]

    @staticmethod
    def _state(application: FleetProfileApplication) -> dict[str, object] | None:
        # Persisted JSON is consumed with the canonical JSON validation
        # semantics, exactly as it was written.  Validating an already-decoded
        # document as strict Python data rejects JSON arrays where the contract
        # declares tuples, which makes Pydantic's smart union pick a different
        # phase-result variant and loses the receipt entirely.
        try:
            progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(application.progress), strict=True
            )
        except ValidationError as error:
            raise FleetProfileConflict(
                "persisted profile application progress is invalid"
            ) from error
        raw = progress.switch_adapter
        if raw is None:
            return None
        return raw.model_dump(mode="json")

    def _save_state(self, application_id: str, state: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            application = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if application is None:
                raise KeyError(application_id)
            progress = _persisted_profile_progress(application)
            if progress.cancellation is not None and progress.switch_adapter is None:
                raise FleetProfileConflict(
                    "Profile cancellation prevents child state creation"
                )
            self._write_state(session, application, state)

    @staticmethod
    def _write_state(
        session: Session,
        application: FleetProfileApplication,
        state: Mapping[str, object],
    ) -> None:
        typed = FleetProfileSwitchAdapterState.model_validate_json(
            canonical_message(state), strict=True
        )
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(application.progress), strict=True
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
        state["state"] = "failed"
        state["status_reason"] = reason[:512]
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_state(application, state)

    @staticmethod
    def _assignments_from_state(
        state: Mapping[str, object],
    ) -> tuple[FleetProfileAssignment, ...]:
        raw = state.get("assignments")
        if not isinstance(raw, list):
            raise FleetProfileConflict(
                "profile switch child has no assignment snapshot"
            )
        try:
            values = tuple(FleetProfileAssignment.model_validate(item) for item in raw)
        except ValueError as error:
            raise FleetProfileConflict(
                "profile switch child assignment snapshot is invalid"
            ) from error
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
                run_switch=RunSwitchOperationResult.model_validate_json(
                    canonical_message(child.result), strict=True
                ),
            )
        except (TypeError, ValueError) as error:
            raise FleetProfileConflict(
                "Run/Switch child result receipt is invalid"
            ) from error

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
            node_ids=_string_items(
                state.get("scope_node_ids", []),
                "profile switch child scope node IDs are invalid",
            ),
            bytes=child.progress.completed_bytes,
            total_bytes=child.progress.total_bytes,
        )
        child_state = _operation_state(child.state, default="running")
        result = self._child_receipt(child)
        return FleetProfileChildOperation(
            id=application_id,
            state=child_state,
            progress=progress,
            status_reason=child.status_reason,
            result=result,
        )

    @staticmethod
    def _view_from_state(
        application: FleetProfileApplication,
        state: Mapping[str, object],
    ) -> FleetProfileChildOperation:
        child_state = _operation_state(state.get("state"), default="running")
        raw_progress = state.get("child_progress")
        progress = (
            # The stored document is JSON that the database driver already
            # decoded, so it must be read with the contract's JSON semantics:
            # Python mode refuses the ISO string this same model serializes for
            # ``start_deadline``, which failed a whole live application after
            # its distribution and install had already succeeded.
            FleetProfileChildProgress.model_validate_json(json.dumps(raw_progress))
            if isinstance(raw_progress, Mapping)
            else FleetProfileChildProgress(
                phase="final-verify" if child_state == "succeeded" else "prepare",
                node_ids=_string_items(
                    state.get("scope_node_ids", []),
                    "profile switch child scope node IDs are invalid",
                ),
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
            return FleetProfileSwitchChildResult.model_validate_json(
                canonical_message(receipt), strict=True
            )
    summary = state.get("result")
    return (
        FleetProfileSwitchAdapterResult.model_validate_json(
            canonical_message(summary), strict=True
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
        raise FleetProfileConflict(
            "Recovery would stop an unreviewed workload; review and load the current profile again"
        )
    if any(
        effect.action == "remove" and _digest(effect) not in reviewed_removals
        for effect in remaining.installations
    ):
        raise FleetProfileConflict(
            "Recovery would remove an unreviewed installation; review and load the current profile again"
        )


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


def _expanded_roles(topology: Mapping[str, object]) -> tuple[tuple[str, bool], ...]:
    raw_roles = topology.get("roles")
    if not isinstance(raw_roles, Sequence) or isinstance(raw_roles, (str, bytes)):
        raise FleetProfileConflict("recipe topology roles are unavailable")
    expanded: list[tuple[str, bool]] = []
    for raw_role in raw_roles:
        if not isinstance(raw_role, Mapping):
            raise FleetProfileConflict("recipe topology role is invalid")
        name = raw_role.get("name")
        count = raw_role.get("count")
        endpoint_owner = raw_role.get("endpoint_owner", False)
        if (
            not isinstance(name, str)
            or not name
            or type(count) is not int
            or not 1 <= count <= 32
            or not isinstance(endpoint_owner, bool)
        ):
            raise FleetProfileConflict("recipe topology role is invalid")
        expanded.extend((name, endpoint_owner) for _ in range(count))
    return tuple(expanded)


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

    @staticmethod
    def _authorize(session: Session, actor: str, *, mutation: bool = True) -> None:
        if mutation:
            serialize_user_authority(session)
        user = session.scalar(select(User).where(User.subject == actor))
        if (
            user is None
            or user.disabled_at is not None
            or (
                mutation
                and user.role
                not in MUTATION_ROLES[("POST", "/api/profile/{number}/load")]
            )
        ):
            raise FleetProfilePermissionDenied(
                "Current profile authority is unavailable"
            )
        try:
            Actor(user.subject, user.role)
        except ValueError:
            raise FleetProfilePermissionDenied(
                "Current profile authority is unavailable"
            ) from None

    def _set_application_state(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: FleetProfileOperationState,
    ) -> None:
        """Change parent state and release only unassigned claims atomically."""
        application.state = state
        release_unassigned_profile_claims(
            session, application, now=_aware(self._clock())
        )

    @contextmanager
    def _admission_session(
        self, actor: str, *, node_ids: Sequence[str] = ()
    ) -> Iterator[Session]:
        """Freeze roster, catalog, workload effects and capacity, including inserts.

        PostgreSQL's implicit writer locks participate, so another owner cannot
        insert or replace an effect between reconciliation and acceptance.
        This is a short SQL-only transaction; contention anywhere in the
        admission work refuses before effects and releases the transaction.
        """
        with self._sessions.begin() as session:
            self._authorize(session, actor)
            try:
                acquire_admission_keys(
                    session,
                    tuple(node_admission_key(node_id) for node_id in node_ids),
                )
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text(
                            "LOCK TABLE agent_nodes, catalog_document_heads, "
                            "catalog_document_revisions, catalog_documents, "
                            "cluster_mapping_nodes, cluster_mappings, "
                            "fleet_profile_applications, installation_nodes, jobs, "
                            "node_inventory_snapshots, recipe_installations, "
                            "recipe_runs, resource_reservations, run_nodes "
                            "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
                        )
                    )
            except AdmissionLockBusy as error:
                raise FleetProfileAdmissionBusy(
                    "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                ) from error
            except OperationalError as error:
                if is_admission_contention(error):
                    raise FleetProfileAdmissionBusy(
                        "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                    ) from None
                raise
            try:
                yield session
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
    def _recipe_identity(session: Session, selector: str) -> tuple[str, str]:
        """Resolve the exact current identities without loading artifact documents."""

        normalized_selector = selector.strip().casefold()
        if normalized_selector.count("/") != 1:
            raise FleetProfileConflict(
                "recipe selector must use canonical publisher/slug form"
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
            raise FleetProfileConflict(
                "recipe selector is not an exact unique active recipe"
            )
        document = candidates[0]
        revision = session.scalar(
            select(CatalogDocumentRevision.id)
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
            .limit(1)
        )
        if revision is None:
            raise FleetProfileConflict("recipe has no active catalog revision")
        return document, revision

    @classmethod
    def _recipe_document(
        cls, session: Session, selector: str
    ) -> tuple[CatalogDocument, CatalogDocumentRevision]:
        document_id, revision_id = cls._recipe_identity(session, selector)
        document = session.get(CatalogDocument, document_id)
        revision = session.get(CatalogDocumentRevision, revision_id)
        if document is None or revision is None:
            raise FleetProfileConflict("recipe catalog changed during resolution")
        return document, revision

    @staticmethod
    def _recipe_selector(document: CatalogDocument) -> str:
        return f"{document.publisher}/{document.slug}"

    def _resolve_choice(
        self, session: Session, choice: FleetProfileAssignmentInput
    ) -> tuple[CatalogDocument, CatalogDocumentRevision, Mapping[str, object] | None]:
        """Resolve one choice once for both presentation and execution."""

        document, revision = self._recipe_document(session, choice.recipe_selector)
        cache: Mapping[str, object] | None = None
        if self._cache_resolver is not None:
            try:
                candidate = self._cache_resolver(
                    recipe_identity=document.id,
                    model_variant=choice.model_variant,
                    exact_revision_id=revision.id,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise FleetProfileConflict(
                    "latest cached profile resolution failed"
                ) from error
            if not isinstance(candidate, Mapping):
                raise FleetProfileConflict(
                    "latest cached profile resolution returned an invalid contract"
                )
            cache = candidate
            recipe_part = candidate.get("recipe")
            chosen_id = (
                recipe_part.get("recipe_revision_id")
                if isinstance(recipe_part, Mapping)
                else None
            )
            # The resolver is asked for the current head revision, so a
            # different revision back is a resolution defect, not an older
            # cached substitute.  Keep that fail-closed rather than silently
            # binding the profile to bytes the operator did not select.
            if isinstance(chosen_id, str) and chosen_id != revision.id:
                chosen = session.get(CatalogDocumentRevision, chosen_id)
                if (
                    chosen is None
                    or chosen.kind != "recipe"
                    or chosen.state != "active"
                    or chosen.document_id != document.id
                ):
                    raise FleetProfileConflict(
                        "cache resolver returned an incompatible recipe revision"
                    )
                raise FleetProfileConflict(
                    "cache resolver replaced the selected recipe revision"
                )
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
    def _choices(row: FleetProfile) -> tuple[FleetProfileAssignmentInput, ...]:
        try:
            return tuple(
                _STORED_ASSIGNMENTS.validate_json(canonical_message(row.assignments))
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise FleetProfileConflict(
                "persisted Fleet profile choices are invalid"
            ) from error

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
            document, revision, _cache = self._resolve_choice(session, choice)
            topology = recipe_topology(revision.document)
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
                if choice.desired_state == "running"
                else None
            )
            result.append(
                FleetProfileAssignment(
                    id=_choice_id(choice),
                    recipe_revision_id=revision.id,
                    topology_name=str(topology.get("name", "unresolved")),
                    desired_state=choice.desired_state,
                    alias=alias,
                    nodes=nodes,
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
                raise KeyError(profile_id)
            return self._view(session, row)

    def get_number(self, number: int) -> FleetProfileView:
        if type(number) is not int or number < 1:
            raise KeyError(number)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
            if row is None:
                raise KeyError(number)
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
                "revision": 1,
                "assignments": [],
            }
            return FleetProfileView(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:profile:{number}")),
                number=number,
                revision=1,
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
        return FleetProfileDefinition.model_validate_json(
            canonical_message(
                {
                    name: getattr(row, name)
                    for name in FleetProfileDefinition.model_fields
                }
            )
        )

    def definition_number(self, number: int) -> FleetProfileDefinitionView:
        """Read authoring intent without consulting catalog, cache, or runtime."""
        if type(number) is not int or number < 1:
            raise KeyError(number)
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
            assignments = self._validated_assignments(session, value.assignments)
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
                raise FleetProfileConflict(
                    "a Fleet profile with this name already exists"
                ) from error
            result = self._view(session, row)
        return result

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
                raise KeyError(profile_id)
            if row.revision != value.expected_revision:
                raise FleetProfileConflict(
                    f"profile revision conflict: expected {value.expected_revision}, current {row.revision}"
                )
            assignments = self._validated_assignments(session, value.assignments)
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
                raise FleetProfileConflict(
                    "a Fleet profile with this name already exists"
                ) from error
            result = self._view(session, row)
        return result

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
                raise KeyError(number)
            if value.expected_revision != 0:
                raise FleetProfileConflict(
                    "profile revision conflict: profile has not been created"
                )
            return self.create(value, actor=actor, number=number)
        return self.update(row.id, value, actor=actor)

    def load(
        self,
        number: int,
        *,
        actor: str,
        request_key: str,
        expected_plan_digest: str,
    ) -> FleetProfileApplicationView:
        """Admit only reviewed intent; replay before consulting mutable choices."""
        with self._sessions() as session:
            profile_id = session.scalar(
                select(FleetProfile.id).where(FleetProfile.number == number)
            )
        if profile_id is None:
            raise KeyError(number)
        return self.apply(
            profile_id,
            plan_digest=expected_plan_digest,
            request_key=request_key,
            actor=actor,
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
                raise KeyError(number)
            return self._application_view(row)

    def endpoint_intent(
        self, session: Session, number: int
    ) -> FleetProfileEndpointIntent:
        """Resolve endpoint membership from the latest immutable application.

        The saved profile is deliberately not consulted: it may have been
        edited since the application whose workloads are currently loaded.
        The operation projection calls this with its own SQL session so the
        profile/application/run ownership read shares one database snapshot.
        """

        if type(number) is not int or number < 1:
            raise KeyError(number)
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
        application = session.scalar(
            select(FleetProfileApplication)
            .where(FleetProfileApplication.profile_id == profile.id)
            .order_by(
                FleetProfileApplication.created_at.desc(),
                FleetProfileApplication.id.desc(),
            )
            .limit(1)
        )
        if application is None:
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=None,
                application_state=None,
                assignments=(),
            )

        application_state = _OPERATION_STATE_ADAPTER.validate_python(
            application.state, strict=True
        )
        try:
            intended = self._intended_profile(application, session=session)
        except (FleetProfileConflict, ValidationError) as error:
            # Broken historical plan or progress blocks execution, but it
            # should not hide the rest of this read-only endpoint projection.
            # The exact reviewed assignments remain unknown; never reconstruct
            # them from today's mutable saved profile.
            cause = error.__cause__
            detail = stored_document_detail(error)
            if detail is None and isinstance(cause, Exception):
                detail = stored_document_detail(cause)
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=application.id,
                application_state=application_state,
                assignments=None,
                projection_issue=FleetProfileEndpointProjectionIssue(
                    code="profile.application_intent.invalid",
                    detail=detail
                    or "Stored application intent is invalid or inconsistent.",
                ),
            )
        projected: list[FleetProfileEndpointAssignmentIntent] = []
        for assignment in intended.assignments:
            if assignment.desired_state == "installed":
                projected.append(
                    FleetProfileEndpointAssignmentIntent(
                        assignment_id=assignment.id,
                        recipe_title=assignment.recipe_title,
                        desired_state="installed",
                        alias=assignment.alias,
                        state="installed-only",
                    )
                )
                continue

            if assignment.alias is None:
                state = "unavailable"
                run_id = None
            else:
                current = self._assignment_state(session, assignment)
                run = current.run
                if (
                    run is not None
                    and run.alias == assignment.alias
                    and run.state == "running"
                    and run.route_state == "published"
                ):
                    state = "not-published-yet"
                    run_id = run.id
                elif run is not None and run.route_state == "pending":
                    state = "not-published-yet"
                    run_id = None
                elif run is not None and run.route_state == "failed":
                    state = "unavailable"
                    run_id = None
                elif application_state == "succeeded":
                    state = "withdrawn"
                    run_id = None
                elif application_state in {"failed", "cancelled"}:
                    state = "unavailable"
                    run_id = None
                else:
                    state = "not-published-yet"
                    run_id = None
            projected.append(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id=assignment.id,
                    recipe_title=assignment.recipe_title,
                    desired_state="running",
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
        allow_pending_cache_rebuild: bool = False,
        profile_application_id: str | None = None,
    ) -> FleetProfilePreview:
        now = _aware(self._clock())
        with self._sessions() as session:
            recovery_images: dict[str, RuntimeImageIdentity] = {}
            if profile_application_id is not None:
                recovery = session.get(FleetProfileApplication, profile_application_id)
                if recovery is None or recovery.profile_id != profile_id:
                    raise FleetProfileConflict("Recovery review owner is unavailable")
                reviewed = self._reviewed_profile_plan(recovery, session=session)
                recovery_images = {
                    item.assignment_id: item.runtime_image
                    for item in reviewed.preparation_decisions
                }
            row = session.get(FleetProfile, profile_id)
            resolved_assignments: tuple[FleetProfileAssignment, ...]
            if row is None:
                if execution_assignments is None:
                    raise KeyError(profile_id)
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
            else:
                view = self._view(session, row)
                resolved_assignments = self._execution_assignments(session, row)
                resolved_name = view.name
                resolved_digest = view.profile_digest
            assignment_previews: list[FleetProfileAssignmentPreview] = []
            assignment_preparations: list[FleetProfileAssignmentPreparation] = []
            assignment_assessments: list[FleetProfileAssignmentAssessment] = []
            reasons: list[FleetProfileReason] = []
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
                row.installation_policy if row is not None else "keep-cached",
                expected_images=recovery_images,
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
                unknown_nodes = sorted(set(expected_nodes) - active_node_ids)
                revision = session.get(
                    CatalogDocumentRevision, assignment.recipe_revision_id
                )
                required_count = None
                if revision is not None:
                    topology = recipe_topology(revision.document)
                    value = topology.get("node_count")
                    required_count = value if type(value) is int else None
                if unknown_nodes:
                    item_reasons.append(
                        FleetProfileReason(
                            code="profile.spark_unavailable",
                            detail=(
                                "Assignment references inactive or unknown Sparks: "
                                + ", ".join(unknown_nodes)
                            ),
                            severity="error",
                        )
                    )
                if required_count is None or len(assignment.nodes) != required_count:
                    item_reasons.append(
                        FleetProfileReason(
                            code="profile.topology_incomplete",
                            detail=(
                                f"Recipe requires {required_count or 'a valid'} Sparks; "
                                f"the draft assigns {len(assignment.nodes)}."
                            ),
                            severity="error",
                        )
                    )
                if self._assessment_provider is None:
                    if not preparation_unavailable_reported:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
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
                            raise TypeError(
                                "The planner returned an invalid assessment."
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
                            raise ValueError(
                                "The planner assessment does not cover the exact assignment scope."
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
                                    code="profile.runtime_image_rebuild_pending",
                                    detail="The accepted runtime image needs cache repair before distribution.",
                                    severity="warning",
                                )
                            )
                        elif preparation is None and requires_preparation:
                            reasons.append(
                                FleetProfileReason(
                                    code="profile.preparation_unavailable",
                                    detail="Prepare the exact model and runtime image in the Controller cache before loading this assignment.",
                                    severity="error",
                                )
                            )
                    except (KeyError, RuntimeError, TypeError, ValueError) as error:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
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
                                code="profile.preparation_unavailable",
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
                                    code="profile.preparation_scope_mismatch",
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
                if state.current_state == assignment.desired_state:
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
                        actions=actions,
                        reasons=item_reasons,
                    )
                )
                reasons.extend(item_reasons)

            if adapter_switch_needed:
                if not changed_nodes or not changed_nodes <= target_nodes:
                    if not any(reason.severity == "error" for reason in reasons):
                        raise FleetProfileConflict(
                            "Profile switch effect scope cannot be represented exactly"
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
                            code="profile.switch_authority_unavailable",
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
                    item.desired_state == "running" and "switch" in item.actions
                    for item in assignment_previews
                ),
                stops=sum(effect.action == "stop" for effect in run_effects),
                uninstalls=sum(
                    effect.action == "remove" for effect in installation_effects
                ),
                blockers=blocker_count,
            )
            effects = control.effects
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
                profile_revision=row.revision if row is not None else None,
                profile_definition=self._definition(row) if row is not None else None,
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
        adapter_switch_needed = False
        for assignment in resolved_assignments:
            state = states[assignment.id]
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
                and assignment.desired_state == "running"
                and state.current_state == "running"
            ):
                desired_run_ids.add(state.run.id)
            if state.current_state != assignment.desired_state:
                adapter_switch_needed = True
                changed_nodes.update(node.node_id for node in assignment.nodes)

        # Every active run intersecting scope is reconciled to the desired
        # running set, independent of installation retention policy.  A
        # distributed run that crosses the boundary is a hard blocker: the
        # controller must never stop only the in-scope ranks.
        active_runs = tuple(
            session.scalars(
                select(RecipeRun)
                .where(RecipeRun.state.in_(_ACTIVE_RUN_STATES))
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
                select(Job).where(Job.state.in_(("queued", "running")))
            ):
                if type(pending.payload.get("workload_intent_ordinal")) is not int:
                    continue
                members = set(pending.targets)
                if not members & target_nodes:
                    continue
                if not members <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code="profile.pending_cross_scope",
                            detail="A pending workload crosses the selected idle scope.",
                            severity="error",
                        )
                    )
                    continue
                adapter_switch_needed = True
                changed_nodes.update(members)
            for pending in session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.state.in_(("queued", "running")),
                    func.coalesce(
                        FleetProfileApplication.progress[
                            "admission_pending"
                        ].as_boolean(),
                        False,
                    ).is_(False),
                )
            ):
                try:
                    _pending_plan = _persisted_profile_plan(pending)
                except FleetProfileConflict:
                    # The step list is unreadable, so the declared frozen
                    # scope is the only durable authority left for which
                    # nodes this order can still affect.  Never infer a
                    # narrower cleanup scope from a damaged document.
                    scope = _persisted_profile_scope(pending)
                    if scope is None:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.pending_record_unreadable",
                                detail=(
                                    "A queued profile change cannot be read and "
                                    "must be reconciled before this profile is "
                                    "applied."
                                ),
                                severity="error",
                            )
                        )
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
                            code="profile.pending_cross_scope",
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
            if not members <= target_nodes:
                reasons.append(
                    FleetProfileReason(
                        code="profile.distributed_cross_scope",
                        detail=(
                            f"Running workload {run.alias} uses Sparks outside "
                            "the profile scope; review the complete distributed group."
                        ),
                        severity="error",
                    )
                )
                continue
            run_effects.append(
                FleetProfileRunEffect(
                    run_id=run.id,
                    installation_id=run.installation_id,
                    alias=run.alias,
                    node_ids=sorted(members),
                    action="keep" if run.id in desired_run_ids else "stop",
                )
            )
            if run_effects[-1].action == "stop":
                adapter_switch_needed = True
                changed_nodes.update(members)

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
                            code="profile.shared_installation_scope",
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
                        code="profile.cleanup_delegated",
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
                    code="profile.interruption_expected",
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
                superseded=cls._pending_effects(session, changed_nodes),
            ),
            changed_nodes=changed_nodes,
            switch_needed=adapter_switch_needed,
            reasons=reasons,
        )

    @staticmethod
    def _pending_effects(
        session: Session, changed_nodes: set[str]
    ) -> list[FleetProfilePendingEffect]:
        """Identify older orders whose effects the new decision supersedes."""
        effects: list[FleetProfilePendingEffect] = []
        if not changed_nodes:
            return effects
        for pending in session.scalars(
            select(Job).where(Job.state.in_(("queued", "running")))
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
                FleetProfileApplication.state.in_(("queued", "running")),
                func.coalesce(
                    FleetProfileApplication.progress["admission_pending"].as_boolean(),
                    False,
                ).is_(False),
            )
        ):
            try:
                plan = _persisted_profile_plan(pending)
                members = {node_id for step in plan.steps for node_id in step.node_ids}
            except FleetProfileConflict:
                # A damaged order retains only its readable frozen authority.
                # Preview's existing blocker handles an unreadable scope.
                members = set(_persisted_profile_scope(pending) or ())
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
        progress = _canonical_progress(row.progress)
        intended = self._intended_profile(row, session=session)
        if (
            row.profile_id != profile_id
            or row.actor != actor
            or progress.retry_of_application_id != retry_of_application_id
            or (
                reviewed_digest is not None
                and intended.reviewed_plan_digest != reviewed_digest
            )
        ):
            raise FleetProfileConflict(
                "Fleet profile request key was reused for another plan"
            )
        return self._application_view(row)

    def _load_replay(
        self,
        profile_id: str,
        *,
        plan_digest: str,
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
                    reviewed_digest=plan_digest,
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
                raise KeyError(preview.profile_id)
            if _digest(_profile_document(profile)) != preview.profile_digest:
                raise FleetProfileStalePlanConflict(
                    "Fleet profile changed before its admission intent was persisted"
                )
            intended = FleetProfileIntendedConfiguration(
                profile_digest=preview.profile_digest,
                reviewed_plan_digest=preview.plan_digest,
                reviewed_application_id=application_id,
                installation_policy=_INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                ),
                scope=FleetProfileScope(node_ids=list(preview.scope.node_ids)),
                assignments=list(preview.resolved_assignments),
            )
            next_retry = now + _admission_retry_delay(1)
            row = FleetProfileApplication(
                id=application_id,
                request_key=request_key,
                profile_id=preview.profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=pending_plan_digest,
                # Keep the short synchronous admission attempt visible as a
                # normal queued application.  If it cannot bind, the defer
                # path changes this to waiting-for-operator before returning.
                state="queued",
                plan=pending_preview.model_dump(mode="json"),
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
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._application_view(row)

    def _defer_pending_application(
        self,
        application_id: str,
        reason: str,
        *,
        retry_delay: timedelta | None = None,
    ) -> FleetProfileApplicationView:
        """Record bounded retry state after a nonblocking admission refusal."""

        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise KeyError(application_id)
            progress = _persisted_profile_progress(row)
            attempt = progress.admission_attempt + 1
            next_retry = now + (
                _admission_retry_delay(attempt) if retry_delay is None else retry_delay
            )
            progress_data = progress.model_dump(mode="json")
            progress_data["admission_pending"] = True
            progress_data["admission_attempt"] = attempt
            progress_data["admission_retry_at"] = next_retry.isoformat()
            row.progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(progress_data), strict=True
            ).model_dump(mode="json")
            row.state = "waiting-for-operator"
            row.status_reason = f"{reason} Next attempt: {next_retry.isoformat()}."[
                :512
            ]
            row.updated_at = now
            session.flush()
            return self._application_view(row)

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
                if (
                    not progress.admission_pending
                    or progress.cancellation is not None
                    or progress.workload_intent_ordinal is not None
                ):
                    return
                profile = session.get(FleetProfile, row.profile_id)
                if profile is None:
                    raise KeyError(row.profile_id)
                if _digest(_profile_document(profile)) != plan.profile_digest:
                    raise FleetProfileStalePlanConflict(
                        "Pending profile intent is stale before workload fencing"
                    )
                acquire_admission_keys(
                    session,
                    tuple(node_admission_key(node_id) for node_id in execution_nodes),
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
                ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
                for node in nodes:
                    node.workload_intent_ordinal = ordinal
                self._switch_adapter.request_superseded_workload_cancellation_in_session(
                    session, execution_nodes, ordinal, now
                )
                progress_data = progress.model_dump(mode="json")
                progress_data["workload_intent_ordinal"] = ordinal
                row.progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
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
        self, profile_id: str, *, plan_digest: str, request_key: str, actor: str
    ) -> FleetProfileApplicationView:
        replay = self._load_replay(
            profile_id, plan_digest=plan_digest, request_key=request_key, actor=actor
        )
        if replay is not None:
            return replay
        pending: FleetProfileApplicationView | None = None
        try:
            preview = self.preview(profile_id)
            if preview.plan_digest != plan_digest:
                raise FleetProfileStalePlanConflict(
                    "Fleet profile preview is stale; review the profile again before loading"
                )
            if not preview.allowed:
                raise FleetProfileConflict(
                    "Fleet profile preview is blocked; review the current blockers before loading"
                )
            pending = self._create_pending_application(
                preview,
                request_key=request_key,
                actor=actor,
                operation_kind="fleet-profile.apply",
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
                except FleetProfileAdmissionBusy:
                    if retry_delay is None:
                        return self._defer_pending_application(
                            pending.id,
                            "Profile admission is busy; the Controller will retry automatically.",
                            retry_delay=timedelta(0),
                        )
                    time.sleep(retry_delay)
                except FleetProfileAdmissionEffectBusy:
                    return self._defer_pending_application(
                        pending.id,
                        "Profile admission is waiting for the active workload owner to finish; the Controller will retry automatically.",
                        retry_delay=timedelta(0),
                    )
            raise FleetProfileAdmissionBusy(
                "Profile admission retry schedule was exhausted"
            )
        except (
            FleetProfileConflict,
            FleetProfilePermissionDenied,
            KeyError,
        ) as error:
            if pending is not None:
                with self._sessions.begin() as session:
                    row = session.get(FleetProfileApplication, pending.id)
                    if (
                        row is not None
                        and _persisted_profile_progress(row).admission_pending
                    ):
                        session.delete(row)
            if isinstance(error, FleetProfilePermissionDenied):
                raise
            # Another identical submission can commit after our first lookup.
            # Its accepted receipt wins over a newly stale preview or a busy
            # admission boundary; this read never refreshes the approved intent.
            replay = self._load_replay(
                profile_id,
                plan_digest=plan_digest,
                request_key=request_key,
                actor=actor,
            )
            if replay is not None:
                if replay.state == "waiting-for-operator":
                    with self._sessions.begin() as session:
                        row = session.get(FleetProfileApplication, replay.id)
                        if (
                            row is not None
                            and _persisted_profile_progress(row).admission_pending
                        ):
                            session.delete(row)
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
        if preview.steps and self._switch_adapter is None:
            raise FleetProfileConflict(
                "Fleet profile Run/Switch authority is unavailable"
            )
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
                raise KeyError(preview.profile_id)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            pending_ordinal: int | None = None
            if existing is not None:
                pending = (
                    pending_application_id == existing.id
                    and _persisted_profile_progress(existing).admission_pending
                )
                if pending:
                    pending_ordinal = _persisted_profile_progress(
                        existing
                    ).workload_intent_ordinal
                if pending and existing.state == "cancelled":
                    raise FleetProfileConflict(
                        "Profile application was superseded by a later intent"
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
            if _digest(_profile_document(profile)) != preview.profile_digest:
                raise FleetProfileStalePlanConflict(
                    "Fleet profile changed during application admission; review again"
                )
            # Use the reviewed snapshot. Resolving through the cache here both
            # substituted newer choices and performed storage work under SQL locks.
            frozen_assignments = tuple(preview.resolved_assignments)
            assignment_by_id = {item.id: item for item in frozen_assignments}
            choices = self._choices(profile)
            if set(assignment_by_id) != {_choice_id(choice) for choice in choices}:
                raise FleetProfileStalePlanConflict(
                    "Profile assignment set changed during admission; review again"
                )
            for choice in choices:
                document_id, revision_id = self._recipe_identity(
                    session, choice.recipe_selector
                )
                assignment = assignment_by_id[_choice_id(choice)]
                if (
                    assignment.recipe_id != document_id
                    or assignment.recipe_revision_id != revision_id
                ):
                    raise FleetProfileStalePlanConflict(
                        "Profile recipe head changed during admission; review again"
                    )
            self._reserve_preview_assets(session, preview, now=now)
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
            intended = FleetProfileIntendedConfiguration(
                profile_digest=preview.profile_digest,
                reviewed_plan_digest=reviewed_plan_digest,
                reviewed_application_id=application_id,
                installation_policy=_INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                ),
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
            attempt = 1
            recovery_ordinal: int | None = None
            accepted_images: dict[str, RuntimeImageIdentity] = {}
            if retry_of_application_id is not None:
                parent = session.get(
                    FleetProfileApplication,
                    retry_of_application_id,
                    with_for_update={"nowait": True},
                )
                if parent is None:
                    raise KeyError(retry_of_application_id)
                prior = _canonical_progress(parent.progress)
                if parent.state not in {"failed", "waiting-for-operator"}:
                    raise FleetProfileConflict(
                        "Only failed or waiting applications can be retried"
                    )
                if parent.profile_digest != intended.profile_digest:
                    raise FleetProfileConflict(
                        "Application intent is obsolete because the saved profile changed"
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
                    other_progress = _canonical_progress(other.progress)
                    if (
                        other_progress.retry_of_application_id == parent.id
                        or other.state in {"queued", "running"}
                        or (other_progress.workload_intent_ordinal or 0)
                        > (prior.workload_intent_ordinal or 0)
                    ):
                        raise FleetProfileConflict(
                            "Application has been superseded by another application"
                        )
                if prior.intended_profile is None:
                    raise FleetProfileConflict(
                        "Persisted application intent is unavailable"
                    )
                if self._superseding_intent(session, parent, prior):
                    raise FleetProfileConflict(
                        "Application has been superseded by another workload intent"
                    )
                intended = self._intended_profile(parent, session=session)
                reviewed_application = session.get(
                    FleetProfileApplication, intended.reviewed_application_id
                )
                if reviewed_application is None:
                    raise FleetProfileConflict(
                        "Persisted application review source is unavailable"
                    )
                _validate_remaining_effects(
                    _persisted_profile_plan(reviewed_application).effects,
                    preview.effects,
                )
                attempt = prior.attempt + 1
                _require_recovery_preparations(
                    _persisted_profile_plan(reviewed_application), preview
                )
                accepted_images = {
                    item.assignment_id: item.runtime_image
                    for item in _persisted_profile_plan(
                        reviewed_application
                    ).preparation_decisions
                }
                if automatic_cache_recovery:
                    if self._switch_adapter is None or not (
                        self._switch_adapter.recoverable_cache_loss(
                            parent.id, session=session
                        )
                    ):
                        raise FleetProfileConflict(
                            "Automatic recovery requires a pre-effect cache loss"
                        )
                    recovery_ordinal = prior.workload_intent_ordinal
                    if recovery_ordinal is None:
                        raise FleetProfileConflict(
                            "Automatic recovery has no bound workload intent"
                        )
            elif automatic_cache_recovery:
                raise FleetProfileConflict("Automatic recovery has no parent receipt")
            control = self._control_effects(
                session,
                frozen_assignments,
                set(frozen_nodes),
                profile.installation_policy,
                expected_images=accepted_images,
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
                node for node in scope_nodes if node.node_id in execution_nodes
            ]
            if self._switch_adapter is not None:
                self._switch_adapter.validate_resources_in_session(
                    session, frozen_assignments, preview
                )
            try:
                lock_profile_build_dependencies(session, preview)
            except BuildConsumerError as error:
                raise FleetProfileConflict(f"{error.code}: {error}") from error
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
                raise FleetProfileStalePlanConflict(
                    "Profile workload intent was superseded before admission resumed"
                )
            if workload_intent_ordinal is not None:
                for node in affected_nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                if self._switch_adapter is None:
                    raise FleetProfileConflict(
                        "Profile switch cancellation authority is unavailable"
                    )
                if pending_ordinal is None:
                    self._switch_adapter.request_superseded_workload_cancellation_in_session(
                        session,
                        tuple(sorted(execution_nodes)),
                        workload_intent_ordinal,
                        now,
                    )
                for prior_application in session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state.in_(
                            ("queued", "running", "waiting-for-operator")
                        )
                    )
                    .order_by(FleetProfileApplication.id)
                    .with_for_update(nowait=True)
                ):
                    try:
                        prior_plan = _persisted_profile_plan(prior_application)
                        prior_scope = {
                            node_id
                            for step in prior_plan.steps
                            for node_id in step.node_ids
                        }
                        if not prior_scope & execution_nodes:
                            continue
                        prior_progress = _persisted_profile_progress(prior_application)
                    except FleetProfileConflict as error:
                        # Quarantine the invalid order, retaining its evidence.
                        # Its agent effects were independently fenced above;
                        # malformed history cannot roll back the new authority.
                        self._set_application_state(
                            session, prior_application, "failed"
                        )
                        prior_application.status_reason = str(error)
                        prior_application.updated_at = now
                        continue
                    prior_ordinal = prior_progress.workload_intent_ordinal
                    if prior_progress.admission_pending:
                        self._set_application_state(
                            session, prior_application, "cancelled"
                        )
                        prior_application.status_reason = (
                            "Profile order was replaced before admission by a later "
                            "scoped intent"
                        )
                        prior_application.updated_at = now
                        continue
                    if (
                        prior_ordinal is None
                        or prior_ordinal >= workload_intent_ordinal
                    ):
                        continue
                    self._set_application_state(session, prior_application, "cancelled")
                    prior_application.status_reason = (
                        "Profile order was replaced by a later scoped intent; "
                        "issued effects retain their own cancellation receipts"
                    )
                    prior_application.updated_at = now
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
                row = FleetProfileApplication(
                    id=application_id,
                    request_key=request_key,
                    profile_id=preview.profile_id,
                    profile_digest=preview.profile_digest,
                    plan_digest=preview.plan_digest,
                    state="succeeded" if not preview.steps else "queued",
                    plan=preview.model_dump(mode="json"),
                    current_step=0,
                    current_operation_id=None,
                    progress=progress,
                    result=(
                        {"changed": False, "completed_steps": 0}
                        if not preview.steps
                        else None
                    ),
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.profile_id = preview.profile_id
                row.profile_digest = preview.profile_digest
                row.plan_digest = preview.plan_digest
                row.state = "succeeded" if not preview.steps else "queued"
                row.plan = preview.model_dump(mode="json")
                row.current_step = 0
                row.current_operation_id = None
                row.progress = progress
                row.result = (
                    {"changed": False, "completed_steps": 0}
                    if not preview.steps
                    else None
                )
                row.status_reason = None
                row.updated_at = now
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
                if item.desired_state == "running"
                and control.states[item.id].current_state != "running"
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
            session.flush()
            return self._application_view(row)

    def retry_eligible(self, application_id: str) -> bool:
        """Whether this receipt is still the current recoverable profile intent."""
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            return row is not None and self._retry_eligible(session, row)

    def _retry_eligible(self, session: Session, row: FleetProfileApplication) -> bool:
        if row.state not in {"failed", "waiting-for-operator"}:
            return False
        try:
            progress = _canonical_progress(row.progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            # A damaged receipt cannot prove that it still carries the current
            # recoverable intent, so it simply does not advertise retry.
            return False
        profile = session.get(FleetProfile, row.profile_id)
        try:
            current_profile_digest = (
                _digest(_profile_document(profile)) if profile is not None else None
            )
        except (FleetProfileConflict, KeyError, TypeError, ValidationError, ValueError):
            return False
        if (
            progress.intended_profile is None
            or current_profile_digest != row.profile_digest
        ):
            return False
        try:
            superseded = self._superseding_intent(session, row, progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            return False
        if superseded:
            return False
        # Retry authority is the durable workload-intent ordinal each node owns
        # (compared above) plus this receipt's explicit lineage.  Decoding every
        # sibling's full progress let one damaged historical row deny a valid
        # receipt its retry; the sibling ordinal was only a proxy for the
        # node-owned intent that is already authoritative.
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

    def retry(
        self,
        application_id: str,
        *,
        request_key: str,
        actor: str,
        automatic_cache_recovery: bool = False,
    ) -> FleetProfileApplicationView:
        """Persist a new reconciliation attempt, retaining the original receipt."""
        with self._sessions() as session:
            self._authorize(session, actor)
            replay = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if replay is not None:
                progress = _canonical_progress(replay.progress)
                if (
                    progress.retry_of_application_id != application_id
                    or replay.actor != actor
                ):
                    raise FleetProfileConflict(
                        "Retry request key was reused for another application"
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
                raise KeyError(application_id)
            progress = _canonical_progress(parent.progress)
            if parent.state not in {"failed", "waiting-for-operator"}:
                raise FleetProfileConflict(
                    "Only failed or waiting applications can be retried"
                )
            if progress.intended_profile is None:
                raise FleetProfileConflict(
                    "Persisted application intent is unavailable"
                )
            adapter = self._switch_adapter
            if parent.current_operation_id is not None:
                if adapter is None:
                    raise FleetProfileConflict(
                        "Current child operation authority is unavailable"
                    )
                try:
                    child = adapter.get(parent.current_operation_id, session=session)
                except (KeyError, RuntimeError, ValueError) as error:
                    raise FleetProfileConflict(
                        "Current child operation state must be reconciled before retry"
                    ) from error
                if child.state not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                    "waiting-for-operator",
                }:
                    raise FleetProfileConflict(
                        "Current child operation is still active"
                    )
            operation_kind = progress.operation_kind or "fleet-profile.apply"
            if operation_kind != "fleet-profile.apply":
                raise FleetProfileConflict(
                    "Only current profile loads can be recovered"
                )
            profile_digest = parent.profile_digest
            persisted_plan = self._reviewed_profile_plan(parent, session=session)
            cache_loss_recovery = (
                adapter is not None
                and adapter.recoverable_cache_loss(parent.id, session=session)
            )
        preview = self.preview(
            persisted_plan.profile_id,
            allow_pending_cache_rebuild=cache_loss_recovery,
            profile_application_id=application_id,
        )
        if preview.profile_digest != profile_digest:
            raise FleetProfileConflict(
                "Application intent is obsolete because the saved profile changed"
            )
        if not preview.allowed:
            raise FleetProfileConflict(
                "Current Fleet state blocks application recovery"
            )
        if tuple(preview.scope.node_ids) != tuple(
            progress.intended_profile.scope.node_ids
        ):
            raise FleetProfileConflict(
                "Fleet scope changed during application recovery"
            )
        expected_assignments = {
            assignment.id: (
                assignment.recipe_revision_id,
                assignment.desired_state,
                tuple(node.node_id for node in assignment.nodes),
            )
            for assignment in progress.intended_profile.assignments
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
            raise FleetProfileConflict(
                "Profile assignment scope changed during application recovery"
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
                    base_statement = base_statement.where(
                        _profile_activity_state_expression() == state
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
                    items=[
                        self._operation_item(
                            row, retry_available=self._retry_eligible(session, row)
                        )
                        for row in rows
                    ],
                    next_cursor=None,
                    total=total,
                )

        def get_operation(operation_id: str) -> Mapping[str, object]:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, operation_id)
                if row is None:
                    raise KeyError(operation_id)
                return self._operation_item(
                    row, retry_available=self._retry_eligible(session, row)
                )

        return OperationProvider(
            family="fleet-profile",
            list_operations=list_operations,
            get_operation=get_operation,
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
        cls, row: FleetProfileApplication, *, retry_available: bool = False
    ) -> dict[str, object]:
        """Project profile progress and its operator-visible failure into Activity.

        A single damaged historical row must not fail the whole page and must
        not be hidden as an empty success: its unreadable document becomes an
        explicit failure on that record while every readable record stays
        usable.
        """

        try:
            typed_progress = _canonical_progress(row.progress)
            plan = _persisted_profile_plan(row)
            result = _persisted_profile_result(row)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            return cls._unreadable_operation_item(row)
        cancellation = _application_cancellation_view(row, plan, typed_progress)
        state = _profile_activity_state(row.state, typed_progress.cancellation)
        failure = None
        if state in {"failed", "waiting-for-operator"}:
            if not row.status_reason or not row.status_reason.strip():
                raise FleetProfileConflict(
                    "Profile application failure reason is missing"
                )
            failure = OperationFailureEvidence(
                error_code="fleet_profile_application_failed",
                summary=(
                    "Profile application needs attention"
                    if state == "waiting-for-operator"
                    else "Profile application failed"
                ),
                detail=redact_text(row.status_reason),
                retryable=retry_available,
                uncertain=state == "waiting-for-operator",
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
            "result": result.model_dump(mode="json") if result is not None else None,
            "cancellation": (
                cancellation.model_dump(mode="json")
                if cancellation is not None
                else None
            ),
            "status_reason": (
                redact_text(row.status_reason)
                if cancellation is not None and row.status_reason is not None
                else None
            ),
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
            "failure": OperationFailureEvidence(
                error_code="fleet_profile_application_unreadable",
                summary="Profile application record is unreadable",
                detail="Persisted Fleet profile plan, progress, or result is invalid",
                retryable=False,
                uncertain=False,
            ).model_dump(mode="json"),
            "result": None,
        }

    def application(self, application_id: str) -> FleetProfileApplicationView:
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise KeyError(application_id)
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
                raise KeyError(application_id)
            intent = _persisted_profile_progress(row).cancellation
            if (
                intent is None
                or intent.cause != "operator"
                or intent.request_key != request_key
                or intent.actor != actor
            ):
                raise KeyError(request_key)
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
                raise KeyError(request_key)
            if (
                number is not None
                and session.scalar(
                    select(FleetProfile.number).where(FleetProfile.id == row.profile_id)
                )
                != number
            ):
                raise KeyError(request_key)
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
            raise FleetProfileConflict(
                "Profile cancellation request key is invalid"
            ) from error
        if str(parsed_key) != request_key:
            raise FleetProfileConflict("Profile cancellation request key is invalid")

        with self._sessions() as snapshot_session:
            self._authorize(snapshot_session, actor)
            snapshot = snapshot_session.get(FleetProfileApplication, application_id)
            if snapshot is None:
                raise KeyError(application_id)
            snapshot_number = snapshot_session.scalar(
                select(FleetProfile.number).where(
                    FleetProfile.id == snapshot.profile_id
                )
            )
            if snapshot_number != profile_number:
                raise KeyError(application_id)
            snapshot_scope = _profile_application_effect_nodes(
                _persisted_profile_plan(snapshot)
            )

        now = _aware(self._clock())
        cancellation: FleetProfileApplicationCancellationIntent | None = None
        with self._admission_session(actor, node_ids=snapshot_scope) as session:
            row = session.get(
                FleetProfileApplication,
                application_id,
                with_for_update={"nowait": True},
            )
            if row is None:
                raise KeyError(application_id)
            if (
                session.scalar(
                    select(FleetProfile.number).where(FleetProfile.id == row.profile_id)
                )
                != profile_number
            ):
                raise KeyError(application_id)

            progress = _persisted_profile_progress(row)
            plan = _persisted_profile_plan(row)
            scope = _profile_application_effect_nodes(plan)
            if scope != snapshot_scope:
                raise FleetProfileConflict(
                    "Profile application scope changed during cancellation"
                )
            previous = progress.cancellation
            if previous is not None:
                if (
                    previous.cause != "operator"
                    or previous.request_key != request_key
                    or previous.actor != actor
                ):
                    raise FleetProfileConflict(
                        "Profile application already has a different cancellation request"
                    )
                cancellation = previous
            else:
                if row.state not in {"queued", "running", "waiting-for-operator"}:
                    raise FleetProfileConflict("Profile application is not cancellable")
                ordinal = progress.workload_intent_ordinal
                if scope and ordinal is None:
                    raise FleetProfileConflict(
                        "Profile cancellation cannot prove its workload intent"
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
                    if any(node.workload_intent_ordinal < ordinal for node in nodes):
                        raise FleetProfileConflict(
                            "Profile workload intent is no longer current"
                        )
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
                            raise FleetProfileConflict(
                                "Profile cancellation authority is unavailable"
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
                row.progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
                ).model_dump(mode="json")
                row.state = "running"
                row.status_reason = (
                    "Cancellation requested; reconciling issued profile effects."
                )
                row.updated_at = now

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
                ("queued", "running", "waiting-for-operator")
            )
            & (cancellation_state == "cancelling")
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
            try:
                plan = _persisted_profile_plan(row)
                current = _persisted_profile_progress(row)
            except FleetProfileConflict as error:
                row.status_reason = (
                    "Cancellation is blocked because persisted effect evidence "
                    f"cannot be reconciled: {str(error)[:360]}"
                )[:512]
                row.updated_at = now
                return True
            intent = current.cancellation
            if intent is None or intent.state != "cancelling":
                return False
            if (
                intent.observation_due_at is not None
                and _aware(intent.observation_due_at) > now
            ):
                return False
            return self._advance_cancellation_in_session(
                session, row, plan, current, now
            )

    def tick(self) -> bool:
        """Observe one due cancellation, then advance one ordinary work item."""

        if self._switch_adapter is None:
            return False
        now = _aware(self._clock())
        pending_admission_observed = self._observe_pending_admissions(now)
        parked_observed = self._observe_parked_applications(now)
        recovery_deferred = False
        cancellation_observed = self._observe_pending_cancellation(now)
        recovery = self._automatic_cache_recovery(now)
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
                        progress = _persisted_profile_progress(row)
                        next_check = now + _cache_recovery_delay(progress.attempt)
                        row.status_reason = (
                            f"{error} Next cache check: {next_check.isoformat()}."
                        )[:512]
                        row.updated_at = now
                        recovery_deferred = True
                # No replacement intent or unknown-output build was admitted.
                # The existing backoff revisits this receipt after cache repair.
            except (FleetProfileConflict, FleetProfilePermissionDenied):
                # Retry performs the authoritative profile, scope, ordinal and
                # lineage checks again after this read-only candidate scan.
                pass
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
                    cancellation_state != "cancelling",
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
            try:
                plan = _persisted_profile_plan(row)
                progress = _persisted_profile_progress(row)
            except FleetProfileConflict as error:
                raw_cancellation = (
                    row.progress.get("cancellation")
                    if isinstance(row.progress, Mapping)
                    else None
                )
                if (
                    isinstance(raw_cancellation, Mapping)
                    and raw_cancellation.get("state") == "cancelling"
                ):
                    row.status_reason = (
                        "Cancellation is blocked because persisted effect evidence "
                        "cannot be reconciled"
                    )
                else:
                    self._set_application_state(session, row, "failed")
                    row.status_reason = str(error)[:512]
                row.updated_at = now
                return True
            if (
                progress.cancellation is not None
                and progress.cancellation.state == "cancelling"
            ):
                # Defensive parity with the SQL exclusion above. Never let a
                # malformed query or dialect quirk monopolize ordinary work.
                return cancellation_observed
            if self._superseding_intent(session, row, progress):
                self._set_application_state(session, row, "cancelled")
                row.status_reason = (
                    "Profile order was replaced by a changed profile or later "
                    "scoped intent; issued effects retain their own cancellation receipts"
                )
                row.updated_at = now
                return True
            steps = [step.model_dump(mode="json") for step in plan.steps]
            if row.current_operation_id:
                try:
                    child = self._switch_adapter.advance(
                        row.current_operation_id, session=session
                    )
                except (KeyError, RuntimeError, ValueError) as error:
                    self._set_application_state(session, row, "failed")
                    row.status_reason = (
                        str(error)[:512] or "Child operation is unavailable"
                    )
                    row.updated_at = now
                    return True
                # The adapter may have checkpointed its child in this same
                # row/transaction. Read that receipt before mirroring progress.
                progress = _persisted_profile_progress(row)
                progress_data = progress.model_dump(mode="json")
                if child.progress is not None:
                    progress_data["child_progress"] = child.progress.model_dump(
                        mode="json"
                    )
                if progress_data != progress.model_dump(mode="json"):
                    progress = FleetProfileApplicationProgress.model_validate_json(
                        canonical_message(progress_data), strict=True
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
                    self._set_application_state(session, row, "running")
                    row.updated_at = now
                    return True
                if child.state in _CHILD_FAILED_STATES:
                    self._set_application_state(
                        session,
                        row,
                        "waiting-for-operator"
                        if child.state == "waiting-for-operator"
                        else "failed",
                    )
                    child_reason = child.status_reason
                    row.status_reason = child_reason or (
                        f"Profile step {row.current_step + 1} ended in {child.state}"
                    )
                    row.updated_at = now
                    return True
                if child.state != "succeeded":
                    self._set_application_state(session, row, "failed")
                    row.status_reason = f"Profile step {row.current_step + 1} returned unsupported state {child.state}"
                    row.updated_at = now
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
                progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
                )
                row.progress = progress.model_dump(mode="json")
                row.current_operation_id = None
                row.current_step += 1
            if row.current_step >= len(steps):
                self._set_application_state(session, row, "succeeded")
                row.status_reason = None
                progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(
                        {
                            **progress.model_dump(mode="json"),
                            "completed_steps": len(steps),
                            "total_steps": len(steps),
                        }
                    ),
                    strict=True,
                )
                row.progress = progress.model_dump(mode="json")
                row.result = {"changed": bool(steps), "completed_steps": len(steps)}
                row.updated_at = now
                return True
            raw_step = steps[row.current_step]
            if not isinstance(raw_step, Mapping):
                self._set_application_state(session, row, "failed")
                row.status_reason = "Persisted Fleet profile step is invalid"
                row.updated_at = now
                return True
            self._set_application_state(session, row, "running")
            progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(
                    {
                        **progress.model_dump(mode="json"),
                        "completed_steps": row.current_step,
                        "total_steps": len(steps),
                        "current_label": raw_step.get("label", "Applying profile"),
                    }
                ),
                strict=True,
            )
            row.progress = progress.model_dump(mode="json")
            row.updated_at = now
            step = dict(raw_step)
            application_id = row.id
            step_index = row.current_step
            actor = row.actor

        try:
            operation_id, synchronous, child = self._start_step(
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
                    else:
                        self._set_application_state(session, failed, "failed")
                        failed.status_reason = (
                            str(error)[:512] or "Profile operation could not be started"
                        )
                    failed.updated_at = _aware(self._clock())
            return True
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
                current.progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
                ).model_dump(mode="json")
            except FleetProfileConflict as error:
                self._set_application_state(session, current, "failed")
                current.status_reason = str(error)[:512]
            current.updated_at = _aware(self._clock())
        return True

    def _observe_pending_admissions(self, now: datetime) -> bool:
        """Retry reviewed applications that could not acquire admission locks."""

        candidate: tuple[str, str, str, FleetProfilePreview] | None = None
        with self._sessions() as session:
            rows = session.scalars(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.state == "waiting-for-operator")
                .order_by(
                    FleetProfileApplication.updated_at.desc(),
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
            )
            for row in rows:
                try:
                    progress = _persisted_profile_progress(row)
                    plan = _persisted_profile_plan(row)
                except FleetProfileConflict:
                    continue
                if not progress.admission_pending or progress.cancellation is not None:
                    continue
                if (
                    progress.admission_retry_at is not None
                    and _aware(progress.admission_retry_at) > now
                ):
                    continue
                candidate = (row.id, row.request_key, row.actor, plan)
                break
        if candidate is None:
            return False
        application_id, request_key, actor, plan = candidate
        try:
            self._prepare_pending_admission(application_id, plan)
            self._queue_application(
                plan,
                request_key=request_key,
                actor=actor,
                operation_kind="fleet-profile.apply",
                pending_application_id=application_id,
            )
        except (FleetProfileAdmissionBusy, FleetProfileAdmissionEffectBusy) as error:
            self._defer_pending_application(application_id, str(error))
            return True
        except FleetProfileStalePlanConflict as error:
            self._finish_pending_admission(
                application_id,
                state="cancelled",
                reason=f"Pending profile intent was superseded: {error}",
            )
            return True
        except (FleetProfileConflict, FleetProfilePermissionDenied, KeyError) as error:
            self._finish_pending_admission(
                application_id,
                state="failed",
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
    ) -> None:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                return
            progress = _persisted_profile_progress(row)
            progress_data = progress.model_dump(mode="json")
            progress_data["admission_pending"] = False
            progress_data["admission_retry_at"] = None
            row.progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(progress_data), strict=True
            ).model_dump(mode="json")
            row.state = state
            row.status_reason = reason[:512]
            row.updated_at = now

    def _observe_parked_applications(self, now: datetime) -> bool:
        """Record the ending of a parked application whose child has ended.

        A ``waiting-for-operator`` order owns no live execution step, so the
        ordinary advancement path never revisits it and its receipt could stay
        parked forever after the operation that parked it terminated.  This is
        the same decision the live path makes, applied to the parked state: a
        superseding intent cancels the order, a terminal child fails it with
        that child's reason, and a child that resumed returns the order to
        ``running`` so the normal advancement continues.  A child that is still
        parked leaves the order exactly as it is.
        """

        adapter = self._switch_adapter
        if adapter is None:
            return False
        with self._sessions.begin() as session:
            admission_pending = func.coalesce(
                FleetProfileApplication.progress["admission_pending"].as_boolean(),
                False,
            )
            rows = tuple(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state == "waiting-for-operator",
                        func.coalesce(
                            FleetProfileApplication.progress["cancellation"][
                                "state"
                            ].as_string(),
                            "",
                        )
                        != "cancelling",
                        admission_pending.is_(False),
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
                try:
                    progress = _persisted_profile_progress(row)
                except FleetProfileConflict as error:
                    row.state = "failed"
                    row.status_reason = str(error)[:512]
                    row.updated_at = now
                    return True
                if self._superseding_intent(session, row, progress):
                    row.state = "cancelled"
                    row.status_reason = (
                        "Profile order was replaced by a changed profile or later "
                        "scoped intent; issued effects retain their own cancellation receipts"
                    )
                    row.updated_at = now
                    return True
                if not row.current_operation_id:
                    continue
                try:
                    child = adapter.get(row.current_operation_id, session=session)
                except (KeyError, RuntimeError, ValueError) as error:
                    row.state = "failed"
                    row.status_reason = (
                        str(error)[:512] or "Child operation is unavailable"
                    )
                    row.updated_at = now
                    return True
                if child.state == "waiting-for-operator":
                    continue
                if child.state in _CHILD_PENDING_STATES:
                    # A resume or an authorised retry made the child live
                    # again; hand the order back to the advancement path.
                    row.state = "running"
                    row.updated_at = now
                    return True
                if child.state in _CHILD_FAILED_STATES:
                    row.state = "failed"
                    row.status_reason = child.status_reason or (
                        f"Profile step {row.current_step + 1} ended in {child.state}"
                    )
                    row.updated_at = now
                    return True
                if child.state != "succeeded":
                    row.state = "failed"
                    row.status_reason = (
                        f"Profile step {row.current_step + 1} returned unsupported "
                        f"state {child.state}"
                    )
                    row.updated_at = now
                    return True
                # The parked child concluded successfully; promote the order so
                # the next tick records its receipt and starts the next step.
                row.state = "running"
                row.updated_at = now
                return True
        return False

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
        row.progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(progress_data), strict=True
        ).model_dump(mode="json")

    def _advance_cancellation_in_session(
        self,
        session: Session,
        row: FleetProfileApplication,
        plan: FleetProfilePreview,
        progress: FleetProfileApplicationProgress,
        now: datetime,
    ) -> bool:
        """Reconcile the exact current child, then finish the parent request."""

        intent = progress.cancellation
        adapter = self._switch_adapter
        if intent is None or adapter is None:
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
                return True
            progress = _persisted_profile_progress(row)
            progress_data = progress.model_dump(mode="json")
            if child.progress is not None:
                progress_data["child_progress"] = child.progress.model_dump(mode="json")
            row.progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(progress_data), strict=True
            ).model_dump(mode="json")
            progress = _persisted_profile_progress(row)
            if child.state == "cancelled" or (
                progress.cancellation is not None
                and progress.cancellation.state == "cancelled"
            ):
                return self._finish_profile_cancellation(session, row, progress, now)
            if (
                child.state in _CHILD_PENDING_STATES
                or child.state == "waiting-for-operator"
            ):
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
                row.state = "running"
                row.status_reason = (
                    child.status_reason
                    or "Cancellation is waiting for the active profile child"
                )[:512]
                row.updated_at = now
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
            row.status_reason = (
                f"Cancellation is waiting for the Run/Switch owner to reconcile "
                f"child state {child.state}"
            )[:512]
            row.updated_at = now
            return True

        if row.current_operation_id is not None:
            self._defer_cancellation_observation(row, progress, now)
            row.status_reason = (
                "Cancellation is waiting for the recorded profile child identity "
                "to become available"
            )
            row.updated_at = now
            return True

        scope = tuple(
            sorted({node_id for step in plan.steps for node_id in step.node_ids})
        )
        if scope and intent.workload_intent_ordinal is not None:
            try:
                effects = AgentJobService.assess_superseded_agent_effects_in_session(
                    session, scope, intent.workload_intent_ordinal, now
                )
            except (TypeError, ValueError) as error:
                self._defer_cancellation_observation(row, progress, now)
                row.status_reason = (
                    "Cancellation is blocked by invalid issued-effect evidence: "
                    f"{str(error)[:360]}"
                )
                row.updated_at = now
                return True
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
                row.progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
                ).model_dump(mode="json")
                owner = (
                    "waiting for issued agent cancellation receipts"
                    if now < deadline
                    else "issued agent cancellation receipt deadline expired; operator action is required"
                )
                row.status_reason = f"Cancellation {owner}: " + ", ".join(operation_ids)
                row.updated_at = now
                return True
        elif scope:
            self._defer_cancellation_observation(row, progress, now)
            row.status_reason = (
                "Cancellation cannot prove the workload intent needed to reconcile "
                "issued effects"
            )
            row.updated_at = now
            return True

        return self._finish_profile_cancellation(session, row, progress, now)

    def _finish_profile_cancellation(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        now: datetime,
    ) -> bool:
        progress_data = progress.model_dump(mode="json")
        cancellation_data = dict(progress_data["cancellation"] or {})
        cancellation_data.update(
            {
                "state": "cancelled",
                "pending_operation_ids": [],
                "observation_due_at": None,
                "observation_deadline_at": None,
            }
        )
        progress_data["cancellation"] = cancellation_data
        row.progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(progress_data), strict=True
        ).model_dump(mode="json")
        row.current_operation_id = None
        self._set_application_state(session, row, "cancelled")
        row.status_reason = (
            "Profile application cancelled after issued effects were reconciled"
        )
        row.result = {
            "changed": bool(progress.completed_steps or progress.step_results),
            "completed_steps": min(progress.completed_steps, row.current_step),
        }
        row.updated_at = now
        return True

    def _automatic_cache_recovery(self, now: datetime) -> tuple[str, str] | None:
        """Find one current failed profile whose only blocker is vanished cache bytes."""

        with self._sessions() as session:
            adapter = self._switch_adapter
            if adapter is None:
                return None
            rows = session.scalars(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.state == "failed")
                .order_by(
                    FleetProfileApplication.updated_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(64)
            )
            for row in rows:
                try:
                    progress = _persisted_profile_progress(row)
                except FleetProfileConflict:
                    continue
                if (
                    progress.intended_profile is None
                    or not adapter.recoverable_cache_loss(row.id, session=session)
                ):
                    continue
                current_scope = tuple(
                    session.scalars(
                        select(AgentNode.node_id)
                        .where(AgentNode.revoked_at.is_(None))
                        .order_by(AgentNode.node_id)
                    )
                )
                if tuple(
                    progress.intended_profile.scope.node_ids
                ) != current_scope or not self._retry_eligible(session, row):
                    continue
                if now < _aware(row.updated_at) + _cache_recovery_delay(
                    progress.attempt
                ):
                    continue
                return row.id, row.actor
        return None

    def _superseding_intent(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Fence unissued profile effects after a newer authorized intent."""

        profile = session.get(FleetProfile, row.profile_id)
        intended = progress.intended_profile
        if (
            profile is None
            or intended is None
            or _digest(_profile_document(profile)) != intended.profile_digest
        ):
            return True
        plan = _persisted_profile_plan(row)
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
    ) -> tuple[str | None, bool, FleetProfileChildOperation | None]:
        kind = step.get("kind")
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"vonk-forge:profile:{application_id}:{step_index}"
            )
        )
        if kind == "switch":
            if self._switch_adapter is None:
                raise FleetProfileConflict(
                    "Fleet profile switch adapter is unavailable"
                )
            execution_scope = tuple(FleetProfilePlanStep.model_validate(step).node_ids)
            if not execution_scope:
                raise FleetProfileConflict("Profile switch effect scope is empty")
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
            if not isinstance(child, FleetProfileChildOperation):
                raise FleetProfileConflict(
                    "Fleet profile switch adapter returned an invalid child operation"
                )
            return child.id, False, child
        raise FleetProfileConflict("Fleet profile step kind is unsupported")

    def _validated_assignments(
        self, session: Session, values: Sequence[FleetProfileAssignmentInput]
    ) -> list[dict[str, object]]:
        assignments: list[dict[str, object]] = []
        for value in values:
            document, _revision = self._recipe_document(session, value.recipe_selector)
            # Save the canonical catalog selector.  This is a logical recipe
            # choice; its active revision is deliberately resolved later.
            normalized = value.model_copy(
                update={"recipe_selector": self._recipe_selector(document)}
            )
            assignments.append(json.loads(canonical_message(normalized)))
        return assignments

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
                _, revision = FleetProfileService._recipe_document(
                    session, assignment.recipe_selector
                )
                model_sets.update(
                    session.scalars(
                        select(ModelCacheSet.artifact_set_sha256).where(
                            ModelCacheSet.recipe_revision_sha256
                            == revision.content_digest
                        )
                    )
                )
                runtime_images.update(
                    session.scalars(
                        select(RuntimeImageAuthorization.oci_archive_sha256).where(
                            RuntimeImageAuthorization.recipe_revision_id == revision.id,
                            RuntimeImageAuthorization.state == "authorized",
                        )
                    )
                )
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
            raise FleetProfileConflict(f"{error.code}: {error.detail}") from error

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
            raise FleetProfileConflict(f"{error.code}: {error.detail}") from error

    def _view(self, session: Session, row: FleetProfile) -> FleetProfileView:
        choices = self._choices(row)
        assignments: list[FleetProfileAssignmentView] = []
        assigned_nodes = {node_id for choice in choices for node_id in choice.spark_ids}
        cache_cached = 0
        cache_missing = 0
        cache_unknown = 0
        warnings: list[str] = []
        for choice in choices:
            recipe, revision, cache = self._resolve_choice(session, choice)
            topology = recipe_topology(revision.document)
            required_sparks = topology.get("node_count")
            required = required_sparks if type(required_sparks) is int else None
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
                blocker == "recipe-not-cached" for blocker in cache_blockers
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
                    observed_state="Not loaded",
                )
            )
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
            status="draft",
            cache_summary={
                "cached": cache_cached,
                "missing": cache_missing,
                "unknown": cache_unknown,
            },
            warnings=sorted(set(warnings)),
            next_actions=[f"vonkctl --profile {row.number} profile load"],
            profile_digest=_digest(document),
            created_by=row.created_by,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
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
        mapping = None
        for candidate in mappings:
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
                current_state="not-placed",
                mapping=None,
                installation=None,
                run=None,
                build=build,
            )
        installation = session.scalar(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.mapping_id == mapping.id,
                RecipeInstallation.recipe_revision_id == assignment.recipe_revision_id,
                RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES),
            )
            .order_by(
                RecipeInstallation.updated_at.desc(), RecipeInstallation.id.desc()
            )
            .limit(1)
        )
        if installation is None:
            return cls._AssignmentState(
                current_state="placed",
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
            installation.state == "installed"
            and len(install_members) == len(expected)
            and {(node.node_id, node.rank, node.role) for node in install_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(node.state == "installed" for node in install_members)
            and (
                installation_matches_runtime_image(
                    installation,
                    build_id=expected_image.build_id,
                    image_digest=expected_image.image_digest,
                    oci_layout_sha256=expected_image.oci_layout_sha256,
                    image_bytes=expected_image.image_bytes,
                )
                if expected_image is not None
                else (
                    build is None
                    or installation_matches_runtime_image(
                        installation,
                        build_id=build.id,
                        image_digest=build.image_digest,
                        oci_layout_sha256=build.oci_layout_sha256,
                        image_bytes=build.image_bytes,
                    )
                )
            )
        )
        if not exact_installed:
            state: FleetProfileAssignmentState = (
                "installing"
                if installation.state in {"planned", "installing"}
                else "degraded"
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
                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
            )
            .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
            .limit(1)
        )
        if run is None:
            return cls._AssignmentState(
                current_state="installed",
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
                installation_ready=True,
            )
        run_members = tuple(
            session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        )
        healthy = (
            run.state == "running"
            and run.route_state == "published"
            and len(run_members) == len(expected)
            and {(node.node_id, node.rank, node.role) for node in run_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(node.state == "running" for node in run_members)
        )
        return cls._AssignmentState(
            current_state="running" if healthy else "degraded",
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
        plan = _persisted_profile_plan(row)
        progress = _canonical_progress(row.progress)
        if (
            row.state in {"queued", "running"}
            and progress.child_progress
            and progress.child_progress.operation
        ):
            progress.child_progress.operation = project_progress(
                progress.child_progress.operation, _aware(self._clock())
            )
        return FleetProfileApplicationView(
            id=row.id,
            request_key=row.request_key,
            profile_id=row.profile_id,
            profile_digest=row.profile_digest,
            plan_digest=row.plan_digest,
            state=_OPERATION_STATE_ADAPTER.validate_python(row.state, strict=True),
            attempt=_canonical_progress(row.progress).attempt,
            retry_of_application_id=_canonical_progress(
                row.progress
            ).retry_of_application_id,
            current_step=row.current_step,
            total_steps=len(plan.steps),
            current_operation_id=row.current_operation_id,
            status_reason=row.status_reason,
            progress=progress,
            cancellation=_application_cancellation_view(row, plan, progress),
            result=_persisted_profile_result(row),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _intended_profile(
        application: FleetProfileApplication,
        *,
        session: Session,
    ) -> FleetProfileIntendedConfiguration:
        progress = _canonical_progress(application.progress)
        if progress.intended_profile is None:
            raise FleetProfileConflict("Persisted application intent is unavailable")
        if progress.intended_profile.profile_digest != application.profile_digest:
            raise FleetProfileConflict(
                "Persisted application intent digest is inconsistent"
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
            raise FleetProfileConflict(
                "Persisted application review source is unavailable"
            )
        root_progress = _canonical_progress(root.progress)
        root_plan = _persisted_profile_plan(root)
        if (
            root_progress.retry_of_application_id is not None
            or root_progress.intended_profile != intended
            or intended.reviewed_application_id != root.id
            or intended.reviewed_plan_digest != _digest(root_plan.reviewed_decision())
        ):
            raise FleetProfileConflict(
                "Persisted application review digest is inconsistent"
            )
        plan = _persisted_profile_plan(application)
        if (
            plan.scope.node_ids != intended.scope.node_ids
            or plan.resolved_assignments
            != sorted(intended.assignments, key=lambda item: item.id)
        ):
            raise FleetProfileConflict(
                "Persisted application plan exceeds its reviewed intent"
            )
        _validate_remaining_effects(root_plan.effects, plan.effects)
        return intended

    @staticmethod
    def _reviewed_profile_plan(
        application: FleetProfileApplication, *, session: Session
    ) -> FleetProfilePreview:
        intended = FleetProfileService._intended_profile(application, session=session)
        reviewed = session.get(
            FleetProfileApplication, intended.reviewed_application_id
        )
        if reviewed is None:
            raise FleetProfileConflict(
                "Persisted application review source is unavailable"
            )
        return _persisted_profile_plan(reviewed)

    def _application_assignments(
        self, application_id: str
    ) -> tuple[FleetProfileAssignment, ...]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            assignments = self._intended_profile(
                application, session=session
            ).assignments
            return tuple(sorted(assignments, key=lambda item: item.id))


__all__ = [
    "FleetProfileAdmissionBusy",
    "FleetProfileAdmissionEffectBusy",
    "FleetProfileConflict",
    "FleetProfileService",
    "FleetProfileStalePlanConflict",
    "RunSwitchFleetProfileAdapter",
]
