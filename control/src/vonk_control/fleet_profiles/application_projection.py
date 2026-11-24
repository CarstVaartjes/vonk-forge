"""Application projection for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy.orm import Session, object_session

from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileChildJobKind,
    ProfileChildPhase,
    ProfileEffectState,
)
from vonk_agent_protocol.lifecycle_vocabulary import (
    InvalidRequestReason,
    LifecycleState,
    ProgressAlarmCode,
)
from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
from vonk_control.categorized_errors import MissingRecord
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationProgress,
    FleetProfileEffectProgress,
    FleetProfileEffectState,
)
from vonk_control.fleet_profile_contract.definitions import FleetProfileAssignment
from vonk_control.fleet_profile_contract.review import (
    FleetProfileApplicationProjectionIssue,
    FleetProfileApplicationView,
    FleetProfilePreview,
)
from vonk_control.fleet_profile_contract.vocabulary import (
    profile_switch_child_request_key,
)
from vonk_control.fleet_profiles.persistence import (
    _intended_profile,
    _persisted_profile_plan,
    _persisted_profile_progress,
    _persisted_profile_result,
    _progress_from_receipt,
)
from vonk_control.fleet_profiles.projection_support import (
    _application_cancellation_view,
    _aware,
)
from vonk_control.fleet_profiles.switch_children import _queue_item_nodes
from vonk_control.jobs import states as job_states
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models.fleet import FleetProfileApplication
from vonk_control.models.jobs import Job
from vonk_control.operations.blockers import MAX_BLOCKERS
from vonk_control.operations.progress import project_progress
from vonk_control.persisted_waits import read_wait
from vonk_control.progress_stall import WAITING_STATES, is_stalled, stalled_blocker
from vonk_control.run_switch_contract.phase_results import RunSwitchStopResult
from vonk_control.run_switch_operations.service import RunSwitchOperationService

if TYPE_CHECKING:
    from vonk_control.fleet_profiles.service import FleetProfileService
    from vonk_control.fleet_profiles.service import (
        FleetProfileService as _FleetProfileService,
    )


class FleetProfileService:
    @staticmethod
    def _effect_progress(
        session: Session,
        row: FleetProfileApplication,
        plan: FleetProfilePreview,
        progress: FleetProfileApplicationProgress,
    ) -> list[FleetProfileEffectProgress]:
        effects: list[FleetProfileEffectProgress] = []
        owners: list[
            tuple[
                FleetProfileApplication,
                FleetProfileApplicationProgress,
                set[str] | None,
            ]
        ] = [(row, progress, None)]
        for binding in plan.effects.adopted:
            owner = session.get(FleetProfileApplication, binding.application_id)
            if owner is not None:
                owners.append(
                    (owner, _persisted_profile_progress(owner), set(binding.node_ids))
                )
        for owner, owner_progress, scope in owners:
            document = owner_progress.switch_adapter
            if document is None or owner_progress.workload_intent_ordinal is None:
                continue
            owner_plan = _persisted_profile_plan(owner)
            if isinstance(owner_plan, Residue):
                continue
            pending = {child.queue_index: child for child in document.pending_children}
            completed = {child.queue_index: child for child in document.children}
            for index, item in enumerate(document.queue):
                nodes = _queue_item_nodes(owner, item)
                if not nodes or (scope is not None and not set(nodes) <= scope):
                    continue
                closed = completed.get(index)
                active = pending.get(index)
                effect_state: FleetProfileEffectState = (
                    closed.state.value
                    if closed
                    else ProfileEffectState.PENDING.value
                    if active
                    else ProfileEffectState.UNKNOWN.value
                    if index in document.skipped_indices
                    else ProfileEffectState.NOT_ISSUED.value
                )
                stop = (
                    next(
                        (
                            effect
                            for effect in owner_plan.effects.runs
                            if effect.run_id == item.id
                            and effect.action == ProfileChildPhase.STOP.value
                        ),
                        None,
                    )
                    if item.kind == ProfileChildPhase.STOP.value
                    else None
                )
                if (
                    stop is not None
                    and closed is not None
                    and closed.state == LifecycleState.SUCCEEDED
                    and (
                        closed.result is None
                        or not any(
                            isinstance(receipt, RunSwitchStopResult)
                            and receipt.run_id == stop.run_id
                            for receipt in closed.result.run_switch.phase_results
                        )
                    )
                ):
                    effect_state = ProfileEffectState.UNKNOWN.value
                record = closed or active
                child_progress = None
                if record is not None:
                    child_job = session.get(Job, record.operation_id)
                    expected_kind = (
                        ProfileChildJobKind.STOP.value
                        if item.kind == ProfileChildPhase.STOP.value
                        else ProfileChildJobKind.CLEANUP.value
                        if item.kind == ProfileChildPhase.CLEANUP.value
                        else ProfileChildJobKind.RUN_SWITCH.value
                    )
                    if (
                        child_job is not None
                        and child_job.kind == expected_kind
                        and child_job.request_id
                        == profile_switch_child_request_key(
                            owner.id, index, item.kind, item.id
                        )
                        and sorted(child_job.targets) == sorted(nodes)
                    ):
                        try:
                            child_progress = RunSwitchOperationService._operation_view(
                                child_job
                            ).progress
                        except TypeError, ValueError:
                            # Damaged measurement is unavailable; queue identity,
                            # exact effects, receipts and claims remain visible.
                            child_progress = None
                effects.append(
                    FleetProfileEffectProgress(
                        effect_id=f"{owner.id}:queue:{index}:{item.kind}:{item.id}",
                        application_id=owner.id,
                        plan_digest=owner.plan_digest,
                        workload_intent_ordinal=owner_progress.workload_intent_ordinal,
                        queue_index=index,
                        kind=item.kind,
                        target_id=item.id,
                        node_ids=list(nodes),
                        request_key=profile_switch_child_request_key(
                            owner.id, index, item.kind, item.id
                        ),
                        operation_id=closed.operation_id
                        if closed
                        else active.operation_id
                        if active
                        else None,
                        state=effect_state,
                        result=closed.result if closed else None,
                        progress=child_progress,
                        stop_effect=stop,
                        original_operation_id=closed.original_operation_id
                        if closed
                        else active.original_operation_id
                        if active
                        else None,
                    )
                )
        return effects

    def _application_view(
        self, row: FleetProfileApplication
    ) -> FleetProfileApplicationView:
        try:
            view = self._stored_application_view(row)
            # Metadata uncertainty must not exempt a success receipt from its
            # invariants (the retained-journal projection can carry an issue).
            if (
                view.projection_issue is not None
                and view.state == LifecycleState.SUCCEEDED
            ):
                FleetProfileApplicationView.model_validate_json(
                    view.model_dump_json(exclude={"projection_issue"})
                )
            return view
        except ValidationError:
            progress = _progress_from_receipt(row)
            detail = "Stored application state is inconsistent; effect unknown"
            return FleetProfileApplicationView(
                wait=read_wait(row),
                id=row.id,
                request_key=row.request_key,
                profile_id=row.profile_id,
                profile_digest=row.profile_digest,
                plan_digest=row.plan_digest,
                state=LifecycleState.CANCELLED,
                attempt=progress.attempt,
                retry_of_application_id=progress.retry_of_application_id,
                current_step=row.current_step,
                total_steps=max(row.current_step, progress.total_steps),
                current_operation_id=row.current_operation_id,
                status_reason=detail,
                progress=progress,
                result=None,
                projection_issue=FleetProfileApplicationProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail=detail,
                ),
                created_at=_aware(row.created_at),
                updated_at=_aware(row.updated_at),
            )

    def _stored_application_view(
        self, row: FleetProfileApplication
    ) -> FleetProfileApplicationView:
        # A damaged plan exposes recorded progress without cancellation authority;
        # a damaged result is unavailable, never reconstructed from a state label.
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        plan = _persisted_profile_plan(row)
        progress = _persisted_profile_progress(row)
        if (
            row.state in {LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}
            and progress.child_progress
            and progress.child_progress.operation
        ):
            progress.child_progress.operation = project_progress(
                progress.child_progress.operation, _aware(self._clock())
            )
        if row.state in WAITING_STATES and is_stalled(
            progress.stall, now=_aware(self._clock())
        ):
            # Shown after the wait it repeats, so that wait stays the lead reason.
            progress.blockers = [
                *progress.blockers[: MAX_BLOCKERS - 1],
                stalled_blocker(progress.stall),
            ]
        wait = read_wait(row)
        if wait is not None:
            alarms = [
                b for b in progress.blockers if b.code is ProgressAlarmCode.STALLED
            ]
            causes = [
                b for b in wait.blockers if b.code is not ProgressAlarmCode.STALLED
            ]
            progress.blockers = [*causes[: MAX_BLOCKERS - len(alarms)], *alarms]
            wait = wait.model_copy(update={"blockers": progress.blockers})
        state, next_attempt_at = self._presented_state(row, progress)
        session = object_session(row)
        if session is not None and not isinstance(plan, Residue):
            progress = progress.model_copy(
                update={"effects": self._effect_progress(session, row, plan, progress)}
            )
        return FleetProfileApplicationView(
            wait=wait,
            id=row.id,
            request_key=row.request_key,
            profile_id=row.profile_id,
            profile_digest=row.profile_digest,
            plan_digest=row.plan_digest,
            state=state,
            attempt=progress.attempt,
            retry_of_application_id=progress.retry_of_application_id,
            superseded_by=progress.superseded_by
            if state == ProfileCancellationCause.SUPERSEDED.value
            else None,
            reason_code=progress.supersede_code
            if state == ProfileCancellationCause.SUPERSEDED.value
            else None,
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
                    LifecycleState.BACKOFF,
                    LifecycleState.FAILED,
                )
                else []
            ),
            next_attempt_at=next_attempt_at,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    def _application_assignments(
        self, application_id: str
    ) -> tuple[FleetProfileAssignment, ...]:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            intended = _intended_profile(application, session=session)
            if isinstance(intended, Residue):
                return ()
            return tuple(sorted(intended.assignments, key=lambda item: item.id))
