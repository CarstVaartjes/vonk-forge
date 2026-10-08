"""Switch observation for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import InvalidRequestReason, LifecycleState, RunSwitchCode
from vonk_agent_protocol.agent_words import (
    ProfileChildPhase,
    ProfileSwitchChildKind,
)

from ..agent_jobs import AgentJobService
from ..categorized_errors import MissingRecord
from ..failure_classification import is_security_failure
from ..fleet_profile_contract import (
    FleetProfileAssignment,
    FleetProfileAssignmentFailure,
    FleetProfileChildOperation,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildState,
    FleetProfileSwitchPendingChild,
    FleetProfileSwitchQueueItem,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..lifecycle.fleet_profile import FleetProfileAdapter, cancellation_state, doc_state
from ..models import STOPPABLE_RUN_STATES, FleetProfileApplication, RecipeRun
from ..run_switch_contract import RunSwitchOperation
from .assessment_support import (
    _stored_state,
)
from .dependencies import _CHILD_PENDING_STATES
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
)

if TYPE_CHECKING:
    from .run_switch_adapter import (
        RunSwitchFleetProfileAdapter as _RunSwitchFleetProfileAdapter,
    )


class RunSwitchFleetProfileAdapter:
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
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface

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
        from .service import FleetProfileService

        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
        application = session.get(FleetProfileApplication, application_id)
        if application is None:
            raise MissingRecord(application_id, reason=InvalidRequestReason.NOT_FOUND)
        state = self._state(application)
        if state is None:
            raise MissingRecord(application_id, reason=InvalidRequestReason.NOT_FOUND)
        progress = _persisted_profile_progress(application)
        cancelling = progress.cancellation is not None
        adopted_scope = FleetProfileService._adopted_application_scope(
            session, application
        )
        pending_scopes: list[set[str]] = []
        lost_children: dict[int, FleetProfileSwitchPendingChild] = {}
        view: FleetProfileChildOperation | None = None
        for pending in list(state.pending_children):
            item = state.queue[pending.queue_index]
            nodes = self._queue_item_nodes(application, item)
            child = self._observed_child(pending.operation_id)
            if child is None:
                # Re-enter exactly the same queue index/request after lost SQL
                # bookkeeping. No changed preview or new effect identity is used.
                if cancelling or (
                    adopted_scope is not None and not set(nodes) <= set(adopted_scope)
                ):
                    # An issued child outside the continuing selection is
                    # still unknown. Retain its one pending queue identity;
                    # neither skipping it nor issuing it again proves absence.
                    pending_scopes.append(set(nodes))
                    continue
                lost_children[pending.queue_index] = pending
                state.position = min(state.position, pending.queue_index)
                continue
            if child.state in _CHILD_PENDING_STATES:
                pending_scopes.append(set(nodes))
                view = self._view_from_child(application_id, state, child)
                state.child_progress = view.progress
                continue
            if child.state not in {
                LifecycleState.SUCCEEDED.value,
                LifecycleState.FAILED.value,
                LifecycleState.CANCELLED.value,
            }:
                pending_scopes.append(set(nodes))
                continue
            state.pending_children.remove(pending)
            receipt = self._child_receipt(child)
            state.children.append(
                FleetProfileSwitchChildState(
                    queue_index=pending.queue_index,
                    operation_id=child.operation_id,
                    kind=pending.kind,
                    state=LifecycleState.SUCCEEDED
                    if child.state == LifecycleState.SUCCEEDED.value
                    else LifecycleState.FAILED
                    if child.state == LifecycleState.FAILED.value
                    else LifecycleState.CANCELLED,
                    result=receipt,
                    original_operation_id=pending.original_operation_id,
                )
            )
            relevant = adopted_scope is None or set(nodes) <= set(adopted_scope)
            partial_stop = (
                pending.kind == ProfileChildPhase.STOP.value
                and item.profile_stop_scope is not None
                and child.result is not None
                and child.result.failure_code
                == RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL
            )
            if (
                relevant
                and not cancelling
                and child.state != LifecycleState.SUCCEEDED.value
                and not partial_stop
            ):
                state.assignment_failures.append(
                    FleetProfileAssignmentFailure(
                        queue_index=pending.queue_index,
                        assignment_id=item.id
                        if item.kind
                        in {
                            ProfileSwitchChildKind.RUN.value,
                            ProfileSwitchChildKind.INSTALL.value,
                        }
                        else None,
                        operation_id=child.operation_id,
                        reason=(
                            child.status_reason
                            or f"Run/Switch child ended in {child.state}"
                        )[:512],
                        terminal=is_security_failure(
                            child.result.failure_code if child.result else None
                        ),
                    )
                )
        self._write_state(session, application, state)
        if cancelling:
            if state.pending_children:
                doc_state(state, LifecycleState.RUNNING)
                self._write_state(session, application, state)
                return view or self._view_from_state(application, state)
            waiting = self._observe_superseded_agent_effects(
                session, application, state
            )
            return waiting or self._finish_cancelled_in_session(
                session, application, state
            )

        occupied = {
            child.queue_index
            for child in (*state.pending_children, *state.children)
            if child.queue_index not in lost_children
        } | set(state.skipped_indices)
        state.position = next(
            (
                index
                for index in range(state.position, len(state.queue))
                if index not in occupied
            ),
            max(state.position, len(state.queue)),
        )
        preceding: list[set[str]] = []
        for index, item in enumerate(state.queue):
            if index in occupied:
                continue
            nodes = self._queue_item_nodes(application, item)
            if not nodes:
                doc_state(state, LifecycleState.RUNNING)
                state.status_reason = (
                    "Waiting for the exact reviewed queue target evidence"
                )
                self._write_state(session, application, state)
                return self._view_from_state(application, state)
            if adopted_scope is not None and not set(nodes) <= set(adopted_scope):
                state.skipped_indices.append(index)
                occupied.add(index)
                continue
            # Preserve dependency order only where full target scopes overlap.
            # A gang Stop intersects every replacement rank, so no half can start.
            if any(set(nodes) & scope for scope in (*pending_scopes, *preceding)):
                preceding.append(set(nodes))
                continue
            if item.kind == ProfileChildPhase.STOP.value:
                run = session.get(RecipeRun, item.id)
                if run is None or run.state not in STOPPABLE_RUN_STATES:
                    # The canonical run owner already reconciled this workload.
                    state.skipped_indices.append(index)
                    occupied.add(index)
                    continue
            with session.no_autoflush:
                waiting = (
                    None
                    if item.kind == ProfileChildPhase.STOP.value
                    else self._observe_superseded_agent_effects(
                        session,
                        application,
                        state,
                        before_dispatch=True,
                        dispatch_scope=nodes,
                    )
                )
            if waiting is not None:
                preceding.append(set(nodes))
                continue
            self._write_state(session, application, state)
            # Accepted effects are maintained by the platform. The author is
            # audit provenance, not a continuing permission dependency.
            operation = self._start_child(
                application_id,
                item,
                assignments,
                nodes,
                state.actor,
                state.request_id,
                index,
                progress.workload_intent_ordinal,
            )
            if isinstance(operation, Residue):
                if operation.reason is BookkeepingReason.EVIDENCE_UNAVAILABLE:
                    state.status_reason = (
                        f"Waiting for exact queue effect: {operation.note}"[:512]
                    )
                    preceding.append(set(nodes))
                    continue
                state.assignment_failures.append(
                    FleetProfileAssignmentFailure(
                        queue_index=index,
                        assignment_id=item.id
                        if item.kind
                        in {
                            ProfileSwitchChildKind.RUN.value,
                            ProfileSwitchChildKind.INSTALL.value,
                        }
                        else None,
                        reason=f"{operation.reason.value}: {operation.note}"[:512],
                    )
                )
                state.skipped_indices.append(index)
                occupied.add(index)
                continue
            if index in lost_children:
                state.pending_children.remove(lost_children[index])
            state.pending_children.append(
                FleetProfileSwitchPendingChild(
                    queue_index=index,
                    operation_id=operation.operation_id,
                    kind=item.kind,
                    original_operation_id=(
                        lost_children[index].original_operation_id
                        or lost_children[index].operation_id
                    )
                    if index in lost_children
                    else None,
                )
            )
            doc_state(state, LifecycleState.RUNNING)
            state.child_progress = self._view_from_child(
                application_id, state, operation
            ).progress
            # Persist one dispatch at a time; the next fair worker pass can issue
            # a disjoint child without holding this child's execution resources.
            self._write_state(session, application, state)
            session.flush()
            return self._view_from_child(application_id, state, operation)
        state.position = next(
            (
                index
                for index in range(state.position, len(state.queue))
                if index not in occupied
            ),
            max(state.position, len(state.queue)),
        )
        self._write_state(session, application, state)
        if state.pending_children or state.position < len(state.queue):
            doc_state(state, LifecycleState.RUNNING)
            self._write_state(session, application, state)
            return view or self._view_from_state(application, state)
        waiting = self._observe_superseded_agent_effects(session, application, state)
        if waiting is not None:
            return waiting
        relevant_children = [
            child
            for child in state.children
            if adopted_scope is None
            or set(self._queue_item_nodes(application, state.queue[child.queue_index]))
            <= set(adopted_scope)
        ]
        relevant_failures = [
            failure
            for failure in state.assignment_failures
            if adopted_scope is None
            or failure.queue_index is None
            or set(
                self._queue_item_nodes(application, state.queue[failure.queue_index])
            )
            <= set(adopted_scope)
        ]
        reviewed = self._child_review(session, application_id)
        incomplete = (
            None
            if isinstance(reviewed, Residue)
            else next(
                (
                    reason
                    for reason in reviewed.reasons
                    if reason.code == RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL
                ),
                None,
            )
        )
        doc_state(
            state,
            LifecycleState.FAILED
            if relevant_failures or incomplete
            else _stored_state(
                FleetProfileAdapter.recorded_aggregate(relevant_children)
            ),
        )
        state.status_reason = (
            incomplete.detail
            if incomplete
            else (
                f"{len(relevant_failures)} assignment(s) need reconciliation: {relevant_failures[0].reason}"[
                    :512
                ]
                if relevant_failures
                else None
            )
        )
        state.result = FleetProfileSwitchAdapterResult(
            children=state.children,
            assignment_ids=[
                item.id
                for item in assignments
                if adopted_scope is None
                or {node.node_id for node in item.nodes} <= set(adopted_scope)
            ],
        )
        if adopted_scope is not None and state.status_reason is None:
            state.status_reason = "Adopted assignments reconciled; other assignments were replaced by the selected profile"
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_state(application, state)

    @staticmethod
    def _queue_item_nodes(
        application: FleetProfileApplication, item: FleetProfileSwitchQueueItem
    ) -> tuple[str, ...]:
        reviewed = _persisted_profile_plan(application)
        progress = _persisted_profile_progress(application)
        if isinstance(reviewed, Residue) or progress.intended_profile is None:
            return ()
        if item.kind in {
            ProfileSwitchChildKind.RUN.value,
            ProfileSwitchChildKind.INSTALL.value,
        }:
            assignment = next(
                (
                    value
                    for value in progress.intended_profile.assignments
                    if value.id == item.id
                ),
                None,
            )
            return (
                tuple(sorted(node.node_id for node in assignment.nodes))
                if assignment
                else ()
            )
        if item.kind == ProfileChildPhase.STOP.value:
            effect = next(
                (
                    value
                    for value in reviewed.effects.runs
                    if value.run_id == item.id
                    and value.action == ProfileChildPhase.STOP.value
                ),
                None,
            )
            if effect is None:
                return ()
            return tuple(
                effect.profile_stop_scope.target_node_ids
                if effect.profile_stop_scope
                else effect.node_ids
            )
        effect = next(
            (
                value
                for value in reviewed.effects.installations
                if value.installation_id == item.id and value.action == "remove"
            ),
            None,
        )
        return tuple(effect.node_ids) if effect else ()

    @staticmethod
    def _queue_item_in_scope(
        session: Session,
        application: FleetProfileApplication,
        item: FleetProfileSwitchQueueItem,
        scope: tuple[str, ...],
    ) -> bool:
        from .run_switch_adapter import RunSwitchFleetProfileAdapter

        del session
        nodes = RunSwitchFleetProfileAdapter._queue_item_nodes(application, item)
        return bool(nodes) and set(nodes) <= set(scope)

    def _observed_child(self, operation_id: str) -> RunSwitchOperation | None:
        """The Run/Switch child, or ``None`` when its record is gone (recorded)."""
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface

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
        state: FleetProfileSwitchAdapterState,
        *,
        before_dispatch: bool = False,
        dispatch_scope: tuple[str, ...] | None = None,
    ) -> FleetProfileChildOperation | None:
        from .service import FleetProfileService

        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
        now = _aware(self._run_switch._clock())
        if (
            not before_dispatch
            and state.state == LifecycleState.RUNNING.value
            and state.observation_due_at is not None
            and now < _aware(state.observation_due_at)
        ):
            return self._view_from_state(application, state)
        progress = _persisted_profile_progress(application)
        cancellation = progress.cancellation
        ordinal = (
            cancellation.workload_intent_ordinal
            if cancellation
            else progress.workload_intent_ordinal
        )
        if ordinal is None:
            return None
        scope = dispatch_scope or tuple(state.scope_node_ids)
        adopted_scope = FleetProfileService._adopted_application_scope(
            session, application
        )
        if cancellation is None and adopted_scope is not None:
            if dispatch_scope is not None and not set(dispatch_scope) <= set(
                adopted_scope
            ):
                state.status_reason = (
                    "Dispatch is waiting for the selected exact continuing scope"
                )
                self._write_state(session, application, state)
                return self._view_from_state(application, state)
            scope = dispatch_scope or adopted_scope
        if before_dispatch:
            with session.no_autoflush:
                effects = AgentJobService.assess_superseded_agent_effects_in_session(
                    session, scope, ordinal, now
                )
        else:
            AgentJobService.abandon_superseded_idempotent_operations_in_session(
                session, scope, ordinal, now
            )
            effects = AgentJobService.assess_superseded_agent_effects_in_session(
                session, scope, ordinal, now
            )
        if not effects:
            if not before_dispatch:
                state.observation_due_at = None
                state.observation_deadline_at = None
                state.pending_operation_ids = []
                state.status_reason = None
                state.stop_reissue_attempt = 0
            return None
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, scope, ordinal, now
        )
        attempt = min(state.stop_reissue_attempt + 1, 32)
        due = max(
            min(effect.observe_due_at for effect in effects),
            FleetProfileAdapter.next_retry(application.id, attempt, now),
        )
        doc_state(state, LifecycleState.RUNNING)
        state.status_reason = (
            "Reissuing Stop and observing older issued workload cancellation: "
            + ", ".join(sorted(effect.operation_id for effect in effects))
        )[:512]
        state.observation_due_at = due
        state.observation_deadline_at = min(
            effect.observation_deadline for effect in effects
        )
        state.pending_operation_ids = sorted(effect.operation_id for effect in effects)
        state.stop_reissue_attempt = attempt
        self._write_state(session, application, state)
        if not before_dispatch:
            session.flush()
        return self._view_from_state(application, state)

    def _finish_cancelled_in_session(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: FleetProfileSwitchAdapterState,
    ) -> FleetProfileChildOperation:
        self = _typing_cast("_RunSwitchFleetProfileAdapter", self)  # noqa: PLW0642 -- assembled mixin interface
        doc_state(state, LifecycleState.CANCELLED)
        state.status_reason = "Profile effects were reconciled after cancellation"
        state.result = FleetProfileSwitchAdapterResult(
            children=state.children, assignment_ids=state.assignment_ids
        )
        self._write_state(session, application, state)
        progress = _persisted_profile_progress(application)
        if progress.cancellation is not None:
            cancellation_state(progress, LifecycleState.CANCELLED)
            application.progress = progress.model_dump(mode="json")
        session.flush()
        return self._view_from_state(application, state)
