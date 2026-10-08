"""Switch projection for Fleet profiles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import InvalidRequestReason, LifecycleState
from vonk_agent_protocol.agent_words import (
    ProfileChildPhase,
    ProfileReasonSeverity,
)

from .. import job_states
from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileChildOperation,
    FleetProfileChildProgress,
    FleetProfileEffects,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchQueueItem,
)
from ..lifecycle.fleet_profile import doc_state
from ..models import FleetProfileApplication
from ..operation_blockers import PHASE_RETRY_CODE, STALL_RETRY_ATTEMPT
from ..preparation_contract import RuntimeImageIdentity
from ..run_switch_contract import RunSwitchOperation
from .assessment_support import (
    _operation_state,
)
from .contracts import (
    FleetProfileInvalid,
)
from .dependencies import _PROFILE_PHASE_ADAPTER, _PROFILE_PHASE_BY_RUN_PHASE
from .persistence import (
    _canonical_progress,
    _persisted_profile_progress,
)
from .projection_support import (
    _state_receipt,
    _switch_queue,
    _validate_remaining_effects,
)

if TYPE_CHECKING:
    from .run_switch_adapter import (
        RunSwitchFleetProfileAdapter as _RunSwitchFleetProfileAdapter,
    )


class RunSwitchFleetProfileAdapter:
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
    ) -> list[FleetProfileSwitchQueueItem]:
        from .service import FleetProfileService

        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
        adopted_assignment_ids = {
            assignment_id
            for effect in reviewed_effects.adopted
            for assignment_id in effect.assignment_ids
        }
        adopted_nodes = {
            node_id
            for effect in reviewed_effects.adopted
            for node_id in effect.node_ids
        }
        assignments = tuple(
            item for item in assignments if item.id not in adopted_assignment_ids
        )
        scope_node_ids = tuple(
            node for node in scope_node_ids if node not in adopted_nodes
        )
        application = session.get(FleetProfileApplication, application_id)
        continuing_scope = (
            None
            if application is None
            else FleetProfileService._adopted_application_scope(session, application)
        )
        if continuing_scope is not None:
            scope_node_ids = tuple(
                node for node in scope_node_ids if node in continuing_scope
            )
            assignments = tuple(
                item
                for item in assignments
                if {node.node_id for node in item.nodes} <= set(continuing_scope)
            )
        control = FleetProfileService._control_effects(
            session,
            assignments,
            set(scope_node_ids),
            installation_policy,
            expected_images=expected_images,
            excluded_application_id=application_id,
        )
        if any(
            reason.severity == ProfileReasonSeverity.ERROR.value
            for reason in control.reasons
        ):
            raise FleetProfileInvalid(
                "Profile workload effects can no longer be represented safely; review again",
                reason=InvalidRequestReason.SUPERSEDED,
            )
        _validate_remaining_effects(reviewed_effects, control.effects)
        return _switch_queue(
            [
                effect
                for effect in control.effects.runs
                if effect.action == ProfileChildPhase.STOP.value
            ],
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
    def _state(
        application: FleetProfileApplication,
    ) -> FleetProfileSwitchAdapterState | None:
        return _persisted_profile_progress(application).switch_adapter

    def _save_state(
        self, application_id: str, state: FleetProfileSwitchAdapterState
    ) -> None:
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
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
        state: FleetProfileSwitchAdapterState,
    ) -> None:
        del session
        progress = _persisted_profile_progress(application)
        progress.switch_adapter = state
        application.progress = _canonical_progress(
            progress.model_dump(mode="json")
        ).model_dump(mode="json")

    def _failed_in_session(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: FleetProfileSwitchAdapterState,
        reason: str,
    ) -> FleetProfileChildOperation:
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
        doc_state(state, LifecycleState.FAILED)
        state.status_reason = reason[:512]
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_state(application, state)

    @staticmethod
    def _assignments_from_state(
        state: FleetProfileSwitchAdapterState, application: FleetProfileApplication
    ) -> tuple[FleetProfileAssignment, ...]:
        del application
        return tuple(state.assignments)

    @staticmethod
    def _child_receipt(
        child: RunSwitchOperation,
    ) -> FleetProfileSwitchChildResult | None:
        return (
            None
            if child.result is None
            else FleetProfileSwitchChildResult(
                run_switch_operation_id=child.operation_id,
                run_switch=child.result,
            )
        )

    def _view_from_child(
        self,
        application_id: str,
        state: FleetProfileSwitchAdapterState,
        child: RunSwitchOperation,
    ) -> FleetProfileChildOperation:
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
        run_phase = (
            child.current_phase
            or child.progress.phase
            or ProfileChildPhase.PREPARE.value
        )
        phase = _PROFILE_PHASE_ADAPTER.validate_python(
            _PROFILE_PHASE_BY_RUN_PHASE.get(run_phase, run_phase), strict=True
        )
        progress = FleetProfileChildProgress(
            operation=child.progress.operation,
            startup_budget_seconds=child.progress.startup_budget_seconds,
            start_deadline=child.progress.start_deadline,
            phase=phase,
            node_ids=state.scope_node_ids,
            bytes=child.progress.completed_bytes,
            total_bytes=child.progress.total_bytes,
        )
        child_state = _operation_state(
            LifecycleState.RUNNING.value
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
        application: FleetProfileApplication, state: FleetProfileSwitchAdapterState
    ) -> FleetProfileChildOperation:
        return FleetProfileChildOperation(
            id=application.id,
            state=state.state,
            progress=state.child_progress
            or FleetProfileChildProgress(
                phase=ProfileChildPhase.FINAL_VERIFY.value
                if state.state == LifecycleState.SUCCEEDED.value
                else ProfileChildPhase.PREPARE.value,
                node_ids=state.scope_node_ids,
            ),
            status_reason=state.status_reason,
            result=_state_receipt(state),
        )
