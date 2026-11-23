"""Projection."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import object_session

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, OutcomeKind
from vonk_agent_protocol.reason_codes.runs import JournalRepairCode
from vonk_control.jobs import states as job_states
from vonk_control.journal_models import RunSwitchJournalRepairPending
from vonk_control.models.jobs import Job
from vonk_control.operations.blockers import make_blocker
from vonk_control.persisted_waits import read_wait, wait_due
from vonk_control.run_switch_contract.journal import RunSwitchJournalRepairPendingState
from vonk_control.run_switch_contract.operations import RunSwitchOperation
from vonk_control.run_switch_contract.progress_view import (
    _progress_operation_state,
    _progress_view,
)
from vonk_control.run_switch_journal_repair.discovery import (
    REPAIR_WAIT,
    is_zero_transfer_journal_fault,
)
from vonk_control.run_switch_operations.constants import _OPERATION_KIND_ADAPTER
from vonk_control.run_switch_operations.plan_persistence import _view_identity
from vonk_control.run_switch_operations.planning_helpers import _stored_job_plan
from vonk_control.run_switch_operations.result_helpers import (
    _parse_persisted_result,
    _read_progress,
)
from vonk_control.stored.rows import read_row_column


class ProjectionMixin:
    @staticmethod
    def _operation_view(job: Job) -> RunSwitchOperation:
        progress = _read_progress(read_row_column(job, "result"))
        persisted_result = _parse_persisted_result(read_row_column(job, "result"))
        # Target membership belongs to the Job; receipts carry member progress.
        plan = _stored_job_plan(job)
        # A plan that cannot be read still leaves the identity the operation was
        # accepted under in the payload: the view is rebuilt from that.
        action, plan_digest, cleanup_mode, installation_id = _view_identity(job, plan)
        current_phase = persisted_result.phase if persisted_result is not None else None
        completed = (
            persisted_result.completed_phases if persisted_result is not None else []
        )
        projected_state = _progress_operation_state(job.state)
        if (
            projected_state == LifecycleState.NEEDS_OPERATOR
            and persisted_result is not None
            and job.next_action_at is not None
        ):
            # Existing accepted rows parked by the prior automatic-observation
            # state are still auto-observed; present their actual behavior.
            projected_state = LifecycleState.OBSERVING
        blockers = (
            list(persisted_result.blockers)
            if persisted_result is not None
            and job.state
            in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.OBSERVING,
                LifecycleState.NEEDS_OPERATOR,
            )
            else []
        )
        if (wait := read_wait(job)) is not None:
            blockers = list(wait.blockers)
        repair_due: datetime | None = None

        if (
            persisted_result is None
            and job.state
            in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
                LifecycleState.OBSERVING.value,
            }
            and is_zero_transfer_journal_fault(job)
        ):
            view_session = object_session(job)
            pending = (
                view_session.get(RunSwitchJournalRepairPending, job.id)
                if view_session is not None
                else None
            )
            retained = (
                read_row_column(pending, "progress") if pending is not None else None
            )
            if isinstance(retained, RunSwitchJournalRepairPendingState):
                repair_due = wait_due(job)
            projected_state = OutcomeKind.UNKNOWN
            blockers = [
                make_blocker(
                    REPAIR_WAIT,
                    job.status_reason
                    or f"{REPAIR_WAIT}: waiting for exact accepted child evidence",
                    node_ids=list(job.targets),
                )
            ]

        if (job.status_reason or "").startswith(JournalRepairCode.EXHAUSTED):
            blockers = [
                make_blocker(
                    JournalRepairCode.EXHAUSTED,
                    job.status_reason or JournalRepairCode.EXHAUSTED,
                    node_ids=list(job.targets),
                )
            ]
        return RunSwitchOperation(
            wait=read_wait(job),
            operation_id=job.id,
            kind=_OPERATION_KIND_ADAPTER.validate_python(job.kind, strict=True),
            action=action,
            state=projected_state,
            plan_digest=plan_digest,
            request_key=job.request_id,
            cleanup_mode=cleanup_mode,
            installation_id=installation_id,
            node_ids=list(job.targets),
            current_phase=current_phase,
            completed_phases=completed,
            progress=_progress_view(
                plan,
                progress,
                projected_state,
                job.status_reason,
                node_ids=job.targets,
            ),
            status_reason=job.status_reason
            if plan is not None or job.status_reason is not None
            else "The stored plan cannot be read; the operation is shown from its "
            "recorded identity.",
            result=persisted_result,
            blockers=blockers,
            next_attempt_at=repair_due or wait_due(job),
        )
