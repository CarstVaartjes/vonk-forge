"""Switch dispatch for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    DesiredAssignmentState,
    InvalidRequestReason,
)
from vonk_agent_protocol.agent_words import (
    ProfileChildJobKind,
    ProfileChildPhase,
    ProfileSwitchChildKind,
)

from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfilePreview,
    FleetProfileSwitchQueueItem,
    profile_switch_child_request_key,
)
from ..job_documents import RunSwitchJobPayload
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..models import FleetProfileApplication, Job
from ..run_switch_contract import (
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
)
from ..stored_json import read_row_column
from .assessment_support import (
    _require_recovery_preparation,
)
from .contracts import (
    FleetProfileChildPlanBlocked,
    FleetProfileInvalid,
    FleetProfileReviewStale,
)
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
)

if TYPE_CHECKING:
    from .run_switch_adapter import (
        RunSwitchFleetProfileAdapter as _RunSwitchFleetProfileAdapter,
    )


class RunSwitchFleetProfileAdapter:
    def _start_child(
        self,
        application_id: str,
        item: FleetProfileSwitchQueueItem,
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
        from .service import FleetProfileService

        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface

        child_request_key = profile_switch_child_request_key(
            application_id, position, item.kind, item.id
        )
        kind = item.kind
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
        if kind == ProfileChildPhase.CLEANUP.value:
            installation_id = item.id
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
            self._validate_child_effects(
                reviewed, cleanup_preview, execution_scope=scope_node_ids
            )
            return self._run_switch.apply_cleanup(
                RunSwitchCleanupApplyRequest(
                    installation_id=installation_id,
                    request_key=child_request_key,
                ),
                actor=actor,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        if kind == ProfileChildPhase.STOP.value:
            run_id = item.id
            if not isinstance(run_id, str):
                return retire_as_unknown(
                    "profile-child",
                    application_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "a stop item has no run identity",
                )
            profile_stop_scope = item.profile_stop_scope
            preview = (
                self._run_switch.preview_stop(
                    RunSwitchStopPreviewRequest(run_id=run_id), actor=actor
                )
                if profile_stop_scope is None
                else self._run_switch.preview_profile_stop(
                    run_id, profile_stop_scope, actor=actor
                )
            )
            self._validate_child_effects(
                reviewed, preview, execution_scope=scope_node_ids
            )
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
        assignment_id = item.id
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
        self._validate_child_effects(reviewed, plan, execution_scope=scope_node_ids)
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
        from .service import FleetProfileService

        application = session.get(FleetProfileApplication, application_id)
        if application is None:
            raise MissingRecord(application_id, reason=InvalidRequestReason.NOT_FOUND)
        intended = FleetProfileService._intended_profile(application, session=session)
        if isinstance(intended, Residue):
            return intended
        return _persisted_profile_plan(application)

    @staticmethod
    def _validate_child_effects(
        reviewed: FleetProfilePreview,
        child: RunSwitchPlan,
        *,
        execution_scope: tuple[str, ...] | None = None,
    ) -> None:
        """A fresh child plan cannot enlarge the accepted parent's consent."""
        execution_nodes = {node for step in reviewed.steps for node in step.node_ids}
        if execution_scope is not None:
            execution_nodes &= set(execution_scope)
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
            if effect.action == ProfileChildPhase.STOP.value
        }
        for stop in child.stops:
            if not set(stop.node_ids) <= execution_nodes:
                raise FleetProfileReviewStale(
                    "Profile child Stop exceeds its current authorized scope"
                )
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
        if child.action == ProfileChildPhase.CLEANUP.value and not any(
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
        item: FleetProfileSwitchQueueItem,
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
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface

        def unknown(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown("profile-child", request_key, reason, note)

        with self._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_key))
            if job is None:
                return None
            kind = item.kind
            if not isinstance(kind, str):
                return unknown(
                    BookkeepingReason.ROW_INCOMPLETE, "the queue item names no kind"
                )
            expected_kind = {
                ProfileChildPhase.CLEANUP.value: ProfileChildJobKind.CLEANUP.value,
                ProfileChildPhase.STOP.value: ProfileChildJobKind.STOP.value,
                ProfileSwitchChildKind.RUN.value: ProfileChildJobKind.RUN_SWITCH.value,
                ProfileSwitchChildKind.INSTALL.value: ProfileChildJobKind.RUN_SWITCH.value,
            }.get(kind)
            if job.kind != expected_kind:
                raise FleetProfileInvalid(
                    "Profile child request key belongs to another operation",
                    reason=InvalidRequestReason.CONFLICT,
                )
            payload = read_row_column(job, "payload")
            if isinstance(payload, Residue):
                return payload
            if not isinstance(payload, RunSwitchJobPayload):
                return unknown(
                    BookkeepingReason.ROW_INCOMPLETE, "the child payload is unavailable"
                )
            recorded_ordinal = payload.workload_intent_ordinal
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
            plan = payload.plan
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
            owner_id = item.id
            if kind == ProfileChildPhase.CLEANUP.value:
                valid = (
                    plan.action == ProfileChildPhase.CLEANUP.value
                    and plan.installation_id == owner_id
                )
            elif kind == ProfileChildPhase.STOP.value:
                expected_scope = item.profile_stop_scope
                valid = (
                    plan.action == ProfileChildPhase.STOP.value
                    and plan.run_id == owner_id
                    and plan.profile_stop_scope == expected_scope
                    and (
                        expected_scope is None
                        or child_nodes == tuple(expected_scope.target_node_ids)
                    )
                )
            else:
                receipt = read_row_column(job, "result")
                if isinstance(receipt, Residue):
                    return receipt
                if not isinstance(receipt, RunSwitchOperationResult):
                    return unknown(
                        BookkeepingReason.ROW_INCOMPLETE,
                        "the child receipt is unavailable",
                    )
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
                        ProfileSwitchChildKind.INSTALL.value
                        if assignment.desired_state == DesiredAssignmentState.INSTALLED
                        else ProfileSwitchChildKind.RUN.value
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
            self._validate_child_effects(reviewed, plan, execution_scope=scope_node_ids)
            operation_id = job.id
        return self._run_switch.get(operation_id)
