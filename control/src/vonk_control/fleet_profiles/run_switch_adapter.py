"""Run switch adapter for Fleet profiles."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DesiredAssignmentState,
    InvalidRequestReason,
    LifecycleState,
    ReservationState,
    RuntimeImageCode,
    UnknownOutcomeError,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileChildPhase,
    ProfileProjectionKind,
    ProfileSwitchChildKind,
)

from .. import job_states
from ..categorized_errors import MissingRecord
from ..failure_classification import is_security_failure
from ..fleet_profile_contract import (
    FleetProfileAdmissionDecision,
    FleetProfileAssignment,
    FleetProfileAssignmentAssessment,
    FleetProfileChildOperation,
    FleetProfilePreview,
    FleetProfileSwitchAdapterState,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..models import (
    CatalogDocumentRevision,
    FleetProfileApplication,
    NodeInventorySnapshot,
    ResourceReservation,
)
from ..preparation_contract import RuntimeImageIdentity
from ..profile_error_summary import _error_summary, _normalized_failure_text
from ..recipe_runtime_specs import resolve_recipe_entities
from ..run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchAssessment,
    RunSwitchOperation,
    RunSwitchPlacementAction,
    SparkGroup,
    SparkGroupNode,
)
from ..run_switch_operations import (
    RunSwitchOperationConflict,
    RunSwitchOperationService,
)
from .assessment_support import (
    _disk_shortfalls,
)
from .contracts import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileInvalid,
    FleetProfileResourceRecheckUnavailable,
)
from .dependencies import _LOGGER, _PROFILE_RECOVERY_REFUSED_CODES
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _stored_recipe,
)
from .switch_dispatch import RunSwitchFleetProfileAdapter as _SwitchDispatch
from .switch_observation import RunSwitchFleetProfileAdapter as _SwitchObservation
from .switch_projection import RunSwitchFleetProfileAdapter as _SwitchProjection


class RunSwitchFleetProfileAdapter(
    _SwitchObservation,
    _SwitchDispatch,
    _SwitchProjection,
):
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
        self, application_id: str, *, request_key: str, actor: str
    ) -> None:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(application)
            cancellation = progress.cancellation
            state = self._state(application)
            if (
                cancellation is None
                or cancellation.request_key != request_key
                or cancellation.actor != actor
                or state is None
            ):
                return
            child_ids = [child.operation_id for child in state.pending_children]
        for child_id in child_ids:
            try:
                self._run_switch.cancel(
                    child_id,
                    actor=actor,
                    request_key=request_key,
                    reason="Profile application cancellation",
                )
            except UnknownOutcomeError:
                # Cancellation intent is already durable. The worker retries
                # these exact children on the next due observation, outside this
                # reading session; the lifecycle cancellation budget ends an
                # unconfirmed stop without parking the application.
                continue
            except (KeyError, RunSwitchOperationConflict):
                continue

    def _failed_children(
        self, application_id: str, *, session: Session
    ) -> tuple[FleetProfileSwitchAdapterState, list[RunSwitchOperation]] | None:
        application = session.get(FleetProfileApplication, application_id)
        state = None if application is None else self._state(application)
        if state is None:
            return None
        operation_ids = {child.operation_id for child in state.pending_children}
        operation_ids.update(
            failure.operation_id
            for failure in state.assignment_failures
            if failure.operation_id is not None
        )
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
        if any(failure.terminal for failure in state.assignment_failures):
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
            for failure in state.assignment_failures:
                parts.add("recorded|" + _normalized_failure_text(failure.reason))
        return "\n".join(sorted(parts)) or None

    def recoverable_cache_loss(self, application_id: str, *, session: Session) -> bool:
        """Recognize only a typed, pre-effect Controller cache loss."""

        found = self._failed_children(application_id, session=session)
        if found is None or found[0].state != LifecycleState.FAILED.value:
            return False
        return any(
            child.state == LifecycleState.FAILED.value
            and child.result is not None
            and child.result.phase == ProfileChildPhase.PREPARE.value
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
        from .service import FleetProfileService

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
                    ProfileProjectionKind.APPLICATION.value,
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
            state = FleetProfileSwitchAdapterState(
                child_id=application_id,
                scope_node_ids=list(scope_node_ids),
                assignment_ids=[item.id for item in ordered_assignments],
                assignments=list(ordered_assignments),
                queue=queue,
                actor=actor,
                request_id=request_id,
            )
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
            for pending in state.pending_children:
                child = self._observed_child(pending.operation_id)
                if child is not None:
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
            ProfileSwitchChildKind.INSTALL.value
            if assignment.desired_state == DesiredAssignmentState.INSTALLED
            else ProfileAction.SWITCH.value,
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
        resolved = resolve_recipe_entities(
            session, _stored_recipe(revision).model_dump(mode="json")
        )
        models = resolved.model_revisions
        model_digest = models[0].content_digest if models else None
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
            return retire_as_unknown(
                "profile-preparation",
                assignment.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "assignment scope changed during preparation observation",
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
        adopted_assignments = {
            assignment_id
            for effect in reviewed.effects.adopted
            for assignment_id in effect.assignment_ids
        }
        node_ids = tuple(
            sorted(
                {
                    node.node_id
                    for assignment in assignments
                    if assignment.id not in adopted_assignments
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
            and item.assignment_id not in adopted_assignments
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
                        if item.kind == ProfileProjectionKind.APPLICATION.value
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
