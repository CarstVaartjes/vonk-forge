"""PostgreSQL authority for saved Fleet profiles and live-versus-desired plans."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

from pydantic import ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .agent_jobs import AgentJobService
from .bounded_json import integer, require_mapping, sequence
from .fleet_profile_contract import (
    FleetProfileAction,
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileApplicationView,
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentPreparation,
    FleetProfileAssignmentPreview,
    FleetProfileAssignmentState,
    FleetProfileAssignmentView,
    FleetProfileChildOperation,
    FleetProfileChildPhase,
    FleetProfileChildProgress,
    FleetProfileChildResult,
    FleetProfileInput,
    FleetProfileInstallationPolicy,
    FleetProfileIntendedConfiguration,
    FleetProfileList,
    FleetProfileNode,
    FleetProfileOperationKind,
    FleetProfileOperationState,
    FleetProfilePlanStep,
    FleetProfilePlanStepKind,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileReason,
    FleetProfileScope,
    FleetProfileScopePreview,
    FleetProfileStepResult,
    FleetProfileSwitchAdapter,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileView,
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
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .operation_contract import OperationFailureEvidence
from .operation_progress import project_progress
from .preparation_contract import RolloutPreparation
from .recipe_operations import RecipeOperationConflict
from .recipe_runtime_specs import (
    recipe_topology,
    resolve_recipe_entities,
)
from .run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchOperation,
    RunSwitchOperationResult,
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
# Decoded and database-sourced closed values are read back through the
# contract's own alias, so a malformed state fails instead of reaching a typed
# model as an unvalidated string.
_OPERATION_STATE_ADAPTER = TypeAdapter(FleetProfileOperationState)
_PROFILE_PHASE_ADAPTER = TypeAdapter(FleetProfileChildPhase)
_INSTALLATION_POLICY_ADAPTER = TypeAdapter(FleetProfileInstallationPolicy)
_MAX_CACHE_RECOVERY_DELAY_SECONDS = 60
#: How many parked applications one worker tick observes for a terminal child.
#: Bounded so a large parked backlog cannot turn one tick into an unbounded
#: scan, while still letting every parked order record its own ending.
_MAX_PARKED_APPLICATION_OBSERVATIONS = 8


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


class _FleetProfileCachePreparationPending(ValueError):
    """A source build is pending and has no verified output identity yet."""


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
            profile = session.get(FleetProfile, application.profile_id)
            queue = self._plan_queue(
                session,
                ordered_assignments,
                scope_node_ids,
                installation_policy=(
                    profile.installation_policy
                    if profile is not None
                    else "keep-cached"
                ),
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
        active = state.get("active_operation_id")
        if isinstance(active, str):
            self._run_switch.tick()
        return self._advance(operation_id, assignments)

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
        return RunSwitchApplyRequest(
            model_content_sha256=model_digest,
            recipe_revision_id=assignment.recipe_revision_id,
            spark_group=group,
            alias=assignment.alias or assignment.recipe_title.lower().replace(" ", "-"),
            action="switch",
            retention="retain-cached",
            plan_digest=None,
            request_key=request_key,
        )

    def preparation(
        self,
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
    ) -> RolloutPreparation | None:
        """Project the exact preparation a profile apply would bind.

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
        plan = self._run_switch.preview(request, actor="controller:profile-preparation")
        if plan.preparation is None:
            if plan.allowed and any(
                phase.kind == "prepare"
                and phase.subphase == "container-build"
                and phase.state == "planned"
                for phase in plan.phases
            ):
                raise _FleetProfileCachePreparationPending(
                    "The exact accepted OCI image will be rebuilt before distribution."
                )
            codes = sorted({reason.code for reason in plan.blockers})[:8]
            detail = (
                "The Run/Switch authority cannot attest exact model and OCI "
                f"preparation evidence for {assignment.recipe_title}"
            )
            if codes:
                detail += " (" + ", ".join(codes) + ")"
            raise ValueError(detail + ".")
        return plan.preparation

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
                reason = child.status_reason or (
                    f"Run/Switch child ended in {child.state}"
                )
                return self._failed_in_session(session, application, state, reason)
            if child.state != "succeeded":
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
        child_request_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk-forge:profile-run-switch:"
                f"{application_id}:{position}:{item.get('kind')}:{item.get('id')}",
            )
        )
        kind = item.get("kind")
        adopted = self._adopt_child(
            child_request_key,
            item=item,
            assignments=assignments,
            scope_node_ids=scope_node_ids,
            workload_intent_ordinal=workload_intent_ordinal,
        )
        if adopted is not None:
            return adopted
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
            accepted = _persisted_profile_plan(application)
            progress = _persisted_profile_progress(application)
            expected_preparation = next(
                (
                    item.preparation
                    for item in accepted.preparations
                    if item.assignment_id == assignment.id
                ),
                None,
            )
        plan = self._run_switch.preview(request, actor=actor)
        if expected_preparation is not None or progress.retry_of_application_id:
            _require_recovery_preparation(
                assignment.id, expected_preparation, plan.preparation
            )
        if not plan.allowed:
            raise RunSwitchOperationConflict(
                "profile child plan blocked: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return self._run_switch.apply(
            request.model_copy(update={"plan_digest": plan.plan_digest}),
            actor=actor,
            workload_intent_ordinal=workload_intent_ordinal,
        )

    def _adopt_child(
        self,
        request_key: str,
        *,
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
                assignment = next(
                    (value for value in assignments if value.id == owner_id), None
                )
                valid = (
                    assignment is not None
                    and plan.action == "switch"
                    and plan.recipe_revision_id == assignment.recipe_revision_id
                    and plan.alias == assignment.alias
                    and child_nodes
                    == tuple(sorted(node.node_id for node in assignment.nodes))
                )
            if not valid:
                raise FleetProfileConflict(
                    "Profile child changed its bound owner or assignment"
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
    ) -> list[dict[str, object]]:
        scope = set(scope_node_ids)
        desired = {
            (
                assignment.recipe_revision_id,
                frozenset(node.node_id for node in assignment.nodes),
            )
            for assignment in assignments
        }
        preserved: set[tuple[str, frozenset[str]]] = set()
        stops: list[dict[str, object]] = []
        for run in session.scalars(
            select(RecipeRun)
            .where(RecipeRun.state.in_(_ACTIVE_RUN_STATES))
            .order_by(RecipeRun.created_at, RecipeRun.id)
        ):
            members = set(self._run_member_ids(session, run))
            if not members.intersection(scope):
                continue
            if not members <= scope:
                raise RunSwitchOperationConflict(
                    "profile switch contains a distributed run outside its "
                    "complete scope"
                )
            installation = session.get(RecipeInstallation, run.installation_id)
            identity = (
                installation.recipe_revision_id if installation is not None else "",
                frozenset(members),
            )
            if (
                self._run_is_healthy(session, run, members)
                and identity in desired
                and identity not in preserved
            ):
                preserved.add(identity)
                continue
            stops.append({"kind": "stop", "id": run.id})
        queue = stops
        for assignment in assignments:
            identity = (
                assignment.recipe_revision_id,
                frozenset(node.node_id for node in assignment.nodes),
            )
            if assignment.desired_state == "running" and identity not in preserved:
                queue.append({"kind": "run", "id": assignment.id})
        # An installation the desired state no longer references is removed by
        # the orchestrator under the same retention decision, so the profile
        # layer never executes a removal itself.  Retention decides whether it
        # is removed at all: ``keep-cached`` retains it.  The scope is
        # authoritative too -- an installation that reaches outside it is not
        # this profile's to remove.
        for installation in (
            session.scalars(
                select(RecipeInstallation)
                .where(RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES))
                .order_by(RecipeInstallation.created_at, RecipeInstallation.id)
            )
            if installation_policy == "exact"
            else ()
        ):
            members = frozenset(_installation_member_ids(session, installation.id))
            if not members or not members <= scope:
                continue
            identity = (installation.recipe_revision_id, members)
            if identity in desired:
                continue
            queue.append({"kind": "cleanup", "id": installation.id})
        return queue

    @staticmethod
    def _run_member_ids(session: Session, run: RecipeRun) -> tuple[str, ...]:
        members = {
            node.node_id
            for node in session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        }
        members.update(
            node.node_id
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == run.installation_id
                )
            )
        )
        return tuple(sorted(members))

    @staticmethod
    def _run_is_healthy(session: Session, run: RecipeRun, members: set[str]) -> bool:
        nodes = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
            )
        )
        return (
            run.state == "running"
            and run.route_state == "published"
            and len(nodes) == len(members)
            and {node.node_id for node in nodes} == members
            and all(node.state == "running" for node in nodes)
        )

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
        preparation_provider=adapter.preparation,
    )


class FleetProfileService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        switch_adapter: FleetProfileSwitchAdapter | None = None,
        cache_resolver: Callable[..., Mapping[str, object]] | None = None,
        preparation_provider: Callable[
            [Session, FleetProfileAssignment, tuple[str, ...]],
            RolloutPreparation | None,
        ]
        | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._switch_adapter = switch_adapter
        self._cache_resolver = cache_resolver
        self._preparation_provider = preparation_provider

    @staticmethod
    def _next_profile_number(session: Session) -> int:
        """Allocate the next stable user profile number without renumbering."""

        maximum = session.scalar(select(func.max(FleetProfile.number)))
        return max(1, int(maximum or 0) + 1)

    @staticmethod
    def _recipe_document(
        session: Session, selector: str
    ) -> tuple[CatalogDocument, CatalogDocumentRevision]:
        """Resolve one exact recipe selector and its newest active revision."""

        normalized_selector = selector.strip().casefold()
        if normalized_selector.count("/") != 1:
            raise FleetProfileConflict(
                "recipe selector must use canonical publisher/slug form"
            )
        publisher, slug = normalized_selector.split("/", 1)
        candidates = tuple(
            session.scalars(
                select(CatalogDocument)
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
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.document_id == document.id,
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
            alias = choice.assignment_name
            if choice.desired_state == "running" and alias is None:
                alias = self._assignment_selector(choice)
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

    def create(
        self, value: FleetProfileInput, *, actor: str, number: int | None = None
    ) -> FleetProfileView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            assignments = self._validated_assignments(session, value.assignments)
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
            if (
                value.expected_revision is not None
                and row.revision != value.expected_revision
            ):
                raise FleetProfileConflict(
                    f"profile revision conflict: expected {value.expected_revision}, current {row.revision}"
                )
            assignments = self._validated_assignments(session, value.assignments)
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
            return self.create(value, actor=actor, number=number)
        return self.update(row.id, value, actor=actor)

    def load(
        self,
        number: int,
        *,
        actor: str,
        request_key: str,
    ) -> FleetProfileApplicationView:
        """Bind fresh intent; only the same request key replays an old load."""

        profile = self.get_number(number)
        with self._sessions() as session:
            replay = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if replay is not None:
                if (
                    replay.profile_id != profile.id
                    or replay.profile_digest != profile.profile_digest
                ):
                    raise FleetProfileConflict(
                        "Load request key was reused for another profile intent"
                    )
                return self._application_view(replay)
        # Recovery follows its own durable request. A new operator request must
        # not depend on decoding history or waiting for an earlier load: the
        # shared admission path fences older intent and reconciles issued effects.
        preview = self.preview(profile.id)
        if not preview.allowed:
            raise FleetProfileConflict("Fleet profile preview is blocked")
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind="fleet-profile.apply",
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

    def preview(
        self,
        profile_id: str,
        *,
        execution_assignments: tuple[FleetProfileAssignment, ...] | None = None,
        profile_name: str | None = None,
        profile_digest: str | None = None,
        allow_pending_cache_rebuild: bool = False,
    ) -> FleetProfilePreview:
        now = _aware(self._clock())
        with self._sessions() as session:
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
            reasons: list[FleetProfileReason] = []
            roster = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            switch_steps: list[_PlanStepDraft] = []
            desired_installation_ids: set[str] = set()
            desired_run_ids: set[str] = set()
            delegated_stop_ids: set[str] = set()
            delegated_cleanup_ids: set[str] = set()
            adapter_switch_needed = False
            changed_nodes: set[str] = set()
            preparation_unavailable_reported = False
            # Scope is the authoritative reconciliation boundary.  An idle
            # member has no assignment and must still participate in the plan.
            target_nodes = {node.node_id for node in roster}

            for assignment in resolved_assignments:
                state = self._assignment_state(session, assignment)
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
                requires_preparation = state.installation is None or (
                    state.current_state in {"not-placed", "placed", "degraded"}
                )
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
                if self._preparation_provider is None:
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
                        preparation = self._preparation_provider(
                            session, assignment, expected_nodes
                        )
                    except _FleetProfileCachePreparationPending as error:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.runtime_image_rebuild_pending",
                                detail=str(error),
                                severity=(
                                    "warning"
                                    if allow_pending_cache_rebuild
                                    else "error"
                                    if requires_preparation
                                    else "warning"
                                ),
                            )
                        )
                    except (KeyError, RuntimeError, ValueError) as error:
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
                if state.installation is not None:
                    desired_installation_ids.add(state.installation.id)
                if (
                    state.run is not None
                    and assignment.desired_state == "running"
                    and state.current_state == "running"
                ):
                    desired_run_ids.add(state.run.id)
                actions: list[FleetProfileAction] = []
                if state.current_state == assignment.desired_state:
                    actions.append("keep")
                else:
                    actions.append("switch")
                    adapter_switch_needed = True
                    changed_nodes.update(expected_nodes)
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
            run_nodes = self._run_nodes(session, [run.id for run in active_runs])
            if not resolved_assignments:
                # An empty assignment set is an explicit all-idle outcome.  If
                # the scope currently contains a run, route reconciliation
                # through the composite child so Run/Switch can stop the
                # complete distributed group exactly once.
                for run in active_runs:
                    members = set(run_nodes.get(run.id, ()))
                    members.update(
                        self._installation_node_ids(session, run.installation_id)
                    )
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
                        FleetProfileApplication.state.in_(("queued", "running"))
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
                members.update(
                    self._installation_node_ids(session, run.installation_id)
                )
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
                if run.id not in desired_run_ids:
                    adapter_switch_needed = True
                    changed_nodes.update(members)
                    delegated_stop_ids.add(run.id)

            installation_policy = (
                row.installation_policy if row is not None else "keep-cached"
            )
            if installation_policy == "exact" and target_nodes:
                installations = tuple(
                    session.scalars(
                        select(RecipeInstallation)
                        .where(RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES))
                        .order_by(RecipeInstallation.created_at, RecipeInstallation.id)
                    )
                )
                installation_nodes = self._installation_nodes(
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
                    delegated_cleanup_ids.add(installation.id)
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
            # A preparation blocker blocks admission.  Preparation reasons carry
            # the run-switch severity vocabulary, whose blocking value is
            # "blocker" rather than this contract's "error", so counting only
            # "error" reported a profile as allowed while its own preparation
            # said the persisted source-build plan is invalid -- and the apply
            # then refused with "profile child plan blocked:
            # run-switch.container-build-plan-invalid" a moment later.  Missing
            # target bytes are not blockers; only a reported blocker is.
            blocker_count = sum(reason.severity == "error" for reason in reasons) + sum(
                1
                for item in assignment_preparations
                for reason in item.preparation.reasons
                if reason.severity == "blocker"
            )
            summary = FleetProfilePlanSummary(
                already_correct=sum(
                    item.actions == ["keep"] for item in assignment_previews
                ),
                placements=sum(step.kind == "create-placement" for step in steps),
                builds=sum(step.kind == "build" for step in steps),
                distributions=sum(step.kind == "distribute-image" for step in steps),
                installs=sum(step.kind == "install" for step in steps),
                starts=sum(
                    item.desired_state == "running" and "switch" in item.actions
                    for item in assignment_previews
                ),
                stops=len(delegated_stop_ids),
                uninstalls=len(delegated_cleanup_ids),
                blockers=blocker_count,
            )
            identity = {
                "schema_version": 2,
                "profile_id": profile_id,
                "profile_digest": resolved_digest,
                "scope": sorted(target_nodes),
                "steps": [step.model_dump(mode="json") for step in steps],
                "assignment_state": [
                    item.model_dump(mode="json") for item in assignment_previews
                ],
                "preparations": [
                    {
                        "assignment_id": item.assignment_id,
                        "preparation": self._preparation_identity(item.preparation),
                    }
                    for item in sorted(
                        assignment_preparations,
                        key=lambda item: item.assignment_id,
                    )
                ],
                "reasons": [reason.model_dump(mode="json") for reason in reasons],
            }
            return FleetProfilePreview(
                profile_id=profile_id,
                profile_name=resolved_name,
                profile_digest=resolved_digest,
                generated_at=now,
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
                preparations=sorted(
                    assignment_preparations,
                    key=lambda item: item.assignment_id,
                ),
                steps=steps,
                reasons=reasons,
                plan_digest=_digest(identity),
            )

    def apply(
        self, profile_id: str, *, plan_digest: str, request_key: str, actor: str
    ) -> FleetProfileApplicationView:
        preview = self.preview(profile_id)
        if not preview.allowed:
            raise FleetProfileConflict("Fleet profile preview is blocked")
        if preview.plan_digest != plan_digest:
            raise FleetProfileConflict("Fleet profile preview is stale")
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind="fleet-profile.apply",
        )

    def _queue_application(
        self,
        preview: FleetProfilePreview,
        *,
        request_key: str,
        actor: str,
        operation_kind: FleetProfileOperationKind,
        retry_of_application_id: str | None = None,
        automatic_cache_recovery: bool = False,
    ) -> FleetProfileApplicationView:
        now = _aware(self._clock())
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
        with self._sessions.begin() as session:
            profile = session.get(
                FleetProfile, preview.profile_id, with_for_update=True
            )
            if profile is None:
                raise KeyError(preview.profile_id)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if existing is not None:
                existing_progress = _canonical_progress(existing.progress)
                if (
                    existing.profile_id != preview.profile_id
                    or existing_progress.retry_of_application_id
                    != retry_of_application_id
                    or existing.plan_digest != preview.plan_digest
                ):
                    raise FleetProfileConflict(
                        "Fleet profile request key was reused for another plan"
                    )
                return self._application_view(existing)
            intended_view = self._view(session, profile)
            frozen_assignments = self._execution_assignments(session, profile)
            scope_nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            frozen_nodes = tuple(node.node_id for node in scope_nodes)
            intended = FleetProfileIntendedConfiguration(
                profile_digest=intended_view.profile_digest,
                installation_policy=_INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                ),
                scope=FleetProfileScope(node_ids=list(frozen_nodes)),
                assignments=list(frozen_assignments),
            )
            if intended.profile_digest != preview.profile_digest:
                raise FleetProfileConflict(
                    "Fleet profile changed during application admission"
                )
            execution_nodes = {
                node_id for step in preview.steps for node_id in step.node_ids
            }
            if not execution_nodes <= set(frozen_nodes):
                raise FleetProfileConflict(
                    "Profile switch scope changed during admission"
                )
            attempt = 1
            recovery_ordinal: int | None = None
            if retry_of_application_id is not None:
                parent = session.get(
                    FleetProfileApplication,
                    retry_of_application_id,
                    with_for_update=True,
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
                intended = prior.intended_profile
                attempt = prior.attempt + 1
                if self._superseding_intent(session, parent, prior):
                    raise FleetProfileConflict(
                        "Application has been superseded by another workload intent"
                    )
                _require_recovery_preparations(_persisted_profile_plan(parent), preview)
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
            affected_nodes = [
                node for node in scope_nodes if node.node_id in execution_nodes
            ]
            workload_intent_ordinal = (
                recovery_ordinal
                if recovery_ordinal is not None
                else max(node.workload_intent_ordinal for node in affected_nodes) + 1
                if affected_nodes
                else None
            )
            if workload_intent_ordinal is not None:
                for node in affected_nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                if self._switch_adapter is None:
                    raise FleetProfileConflict(
                        "Profile switch cancellation authority is unavailable"
                    )
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
                    .with_for_update()
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
                        prior_application.state = "failed"
                        prior_application.status_reason = str(error)
                        prior_application.updated_at = now
                        continue
                    prior_ordinal = prior_progress.workload_intent_ordinal
                    if (
                        prior_ordinal is None
                        or prior_ordinal >= workload_intent_ordinal
                    ):
                        continue
                    prior_application.state = "cancelled"
                    prior_application.status_reason = (
                        "Profile order was replaced by a later scoped intent; "
                        "issued effects retain their own cancellation receipts"
                    )
                    prior_application.updated_at = now
            row = FleetProfileApplication(
                request_key=request_key,
                profile_id=preview.profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=preview.plan_digest,
                state="succeeded" if not preview.steps else "queued",
                plan=preview.model_dump(mode="json"),
                current_step=0,
                current_operation_id=None,
                progress=FleetProfileApplicationProgress(
                    operation_kind=operation_kind,
                    attempt=attempt,
                    retry_of_application_id=retry_of_application_id,
                    intended_profile=intended,
                    workload_intent_ordinal=workload_intent_ordinal,
                    completed_steps=0,
                    total_steps=len(preview.steps),
                ).model_dump(mode="json"),
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
                self._view(session, profile).profile_digest
                if profile is not None
                else None
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
            replay = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if replay is not None:
                progress = _canonical_progress(replay.progress)
                if progress.retry_of_application_id != application_id:
                    raise FleetProfileConflict(
                        "Retry request key was reused for another application"
                    )
                return self._application_view(replay)
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
                    child = adapter.get(parent.current_operation_id)
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
            persisted_plan = _persisted_profile_plan(parent)
            cache_loss_recovery = (
                adapter is not None
                and adapter.recoverable_cache_loss(parent.id, session=session)
            )
        preview = self.preview(
            persisted_plan.profile_id,
            allow_pending_cache_rebuild=cache_loss_recovery,
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
        from .operation_api import OperationListPage, OperationProvider, OperationQuery

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
                        FleetProfileApplication.state == state
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
                if after is not None:
                    after_at, after_id = after
                    statement = statement.where(
                        (FleetProfileApplication.created_at < after_at)
                        | (
                            (FleetProfileApplication.created_at == after_at)
                            & (FleetProfileApplication.id < after_id)
                        )
                    )
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
        failure = None
        if row.state in {"failed", "waiting-for-operator"}:
            if not row.status_reason or not row.status_reason.strip():
                raise FleetProfileConflict(
                    "Profile application failure reason is missing"
                )
            failure = OperationFailureEvidence(
                error_code="fleet_profile_application_failed",
                summary=(
                    "Profile application needs attention"
                    if row.state == "waiting-for-operator"
                    else "Profile application failed"
                ),
                detail=redact_text(row.status_reason),
                retryable=retry_available,
                uncertain=row.state == "waiting-for-operator",
            ).model_dump(mode="json")
        return {
            "id": row.id,
            "parent_id": typed_progress.retry_of_application_id,
            "node_ids": list(cls._operation_scope(plan)),
            "kind": typed_progress.operation_kind or "fleet-profile.apply",
            "state": row.state,
            "attempt": typed_progress.attempt,
            "progress": {"phase": cls._operation_phase(row, plan, typed_progress)},
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "supported_actions": ["retry"] if retry_available else [],
            "failure": failure,
            "result": result.model_dump(mode="json") if result is not None else None,
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

    def application_by_request_key(
        self, request_key: str
    ) -> FleetProfileApplicationView:
        """Resolve one accepted submission after its response was lost."""

        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if row is None:
                raise KeyError(request_key)
            return self._application_view(row)

    def tick(self) -> bool:
        """Observe bounded parked work, then advance at most one runnable step."""

        if self._switch_adapter is None:
            return False
        now = _aware(self._clock())
        # Complete the bounded observation transaction before selecting active
        # work. An unrelated active application must not starve a recovered
        # parent, and observing a parent must not consume the active work unit.
        parked_observed = self._observe_parked_applications(now)
        recovery_deferred = False
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
            except FleetProfileConflict:
                # Retry performs the authoritative profile, scope, ordinal and
                # lineage checks again after this read-only candidate scan.
                pass
            else:
                return True
        with self._sessions.begin() as session:
            row = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.state.in_(("queued", "running")))
                .order_by(
                    FleetProfileApplication.created_at, FleetProfileApplication.id
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return parked_observed or recovery_deferred
            try:
                plan = _persisted_profile_plan(row)
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
            steps = [step.model_dump(mode="json") for step in plan.steps]
            if row.current_operation_id:
                try:
                    child = self._switch_adapter.get(
                        row.current_operation_id, session=session
                    )
                except (KeyError, RuntimeError, ValueError) as error:
                    row.state = "failed"
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
                        return parked_observed or recovery_deferred
                    row.state = "running"
                    row.updated_at = now
                    return True
                if child.state in _CHILD_FAILED_STATES:
                    row.state = (
                        "waiting-for-operator"
                        if child.state == "waiting-for-operator"
                        else "failed"
                    )
                    child_reason = child.status_reason
                    row.status_reason = child_reason or (
                        f"Profile step {row.current_step + 1} ended in {child.state}"
                    )
                    row.updated_at = now
                    return True
                if child.state != "succeeded":
                    row.state = "failed"
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
                row.state = "succeeded"
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
                row.state = "failed"
                row.status_reason = "Persisted Fleet profile step is invalid"
                row.updated_at = now
                return True
            row.state = "running"
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
                    failed.state = "failed"
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
                current.state = "failed"
                current.status_reason = str(error)[:512]
            current.updated_at = _aware(self._clock())
        return True

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
            rows = tuple(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(FleetProfileApplication.state == "waiting-for-operator")
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
            or self._view(session, profile).profile_digest != intended.profile_digest
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

        def __init__(
            self,
            *,
            current_state: FleetProfileAssignmentState,
            mapping: ClusterMapping | None,
            installation: RecipeInstallation | None,
            run: RecipeRun | None,
            build: RecipeBuild | None,
        ) -> None:
            self.current_state = current_state
            self.mapping = mapping
            self.installation = installation
            self.run = run
            self.build = build

    @staticmethod
    def _preparation_identity(preparation: RolloutPreparation) -> dict[str, object]:
        identity = preparation.model_dump(mode="json")
        for asset_name in ("model", "runtime_image"):
            asset = identity.get(asset_name)
            if not isinstance(asset, dict):
                continue
            controller = asset.get("controller")
            if isinstance(controller, dict):
                controller.pop("verified_at", None)
                controller.pop("state", None)
                controller.pop("verified_bytes", None)
                controller.pop("missing_bytes", None)
            targets = asset.get("targets")
            if isinstance(targets, list):
                for target in targets:
                    if isinstance(target, dict):
                        target.pop("verified_at", None)
                        target.pop("state", None)
                        target.pop("present_bytes", None)
                        target.pop("missing_bytes", None)
                        target.pop("verified_sha256", None)
                        target.pop("imported_image_digest", None)
                        target.pop("reason", None)
        identity.pop("controller_ready", None)
        identity.pop("targets_ready", None)
        identity.pop("ready", None)
        identity.pop("reasons", None)
        return identity

    def _assignment_state(
        self, session: Session, assignment: FleetProfileAssignment
    ) -> _AssignmentState:
        build = session.scalar(
            select(RecipeBuild)
            .where(
                RecipeBuild.recipe_revision_id == assignment.recipe_revision_id,
                RecipeBuild.state == "succeeded",
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
            .limit(1)
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
            return self._AssignmentState(
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
            return self._AssignmentState(
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
        )
        if not exact_installed:
            state: FleetProfileAssignmentState = (
                "installing"
                if installation.state in {"planned", "installing"}
                else "degraded"
            )
            return self._AssignmentState(
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
            return self._AssignmentState(
                current_state="installed",
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
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
        return self._AssignmentState(
            current_state="running" if healthy else "degraded",
            mapping=mapping,
            installation=installation,
            run=run,
            build=build,
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
            result=_persisted_profile_result(row),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _intended_profile(
        application: FleetProfileApplication,
    ) -> FleetProfileIntendedConfiguration:
        progress = _canonical_progress(application.progress)
        if progress.intended_profile is None:
            raise FleetProfileConflict("Persisted application intent is unavailable")
        if progress.intended_profile.profile_digest != application.profile_digest:
            raise FleetProfileConflict(
                "Persisted application intent digest is inconsistent"
            )
        return progress.intended_profile

    def _application_assignments(
        self, application_id: str
    ) -> tuple[FleetProfileAssignment, ...]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            assignments = self._intended_profile(application).assignments
            return tuple(sorted(assignments, key=lambda item: item.id))


__all__ = [
    "FleetProfileConflict",
    "FleetProfileService",
    "RunSwitchFleetProfileAdapter",
]
