"""Run switch journal repair: pending."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    canonical_message,
)

from ..models import (
    Job,
    RunSwitchJournalRepairPending,
)
from ..run_switch_contract import (
    RunSwitchCancellation,
    RunSwitchOperationResult,
)
from ..run_switch_journal_contract import (
    RunSwitchJournalRepairPendingState,
)
from .discovery import _candidate

# A damaged observation gets the same bounded window across restarts. The
# interval is capped; neither a changed sample nor a new caller resets it.
REPAIR_BUDGET = timedelta(minutes=2)


def _pending(
    session: Session, job: Job, now: datetime
) -> tuple[RunSwitchJournalRepairPending, RunSwitchJournalRepairPendingState]:
    row = session.get(RunSwitchJournalRepairPending, job.id)
    if row is None:
        state = RunSwitchJournalRepairPendingState(
            deadline_at=now + REPAIR_BUDGET, next_attempt_at=now
        )
        row = RunSwitchJournalRepairPending(
            job_id=job.id, deadline_at=state.deadline_at, progress=state
        )
        session.add(row)
    else:
        try:
            state = RunSwitchJournalRepairPendingState.model_validate_json(
                canonical_message(row.progress), strict=True
            )
        except (TypeError, ValueError):
            # The deadline column owns the clock independently of this mutable
            # projection. Cancellation belongs to its independent typed column.
            progress = _candidate(job)
            state = RunSwitchJournalRepairPendingState(
                deadline_at=row.deadline_at,
                next_attempt_at=now,
                cancellation=progress.cancellation if progress is not None else None,
            )
            row.progress = state
    state.deadline_at = row.deadline_at
    # Mutable progress never owns acknowledged cancellation. Its independent
    # typed column survives corruption of the observation projection.
    if isinstance(row.cancellation, RunSwitchCancellation):
        state.cancellation = row.cancellation
    return row, state


def record_repair_cancellation(
    sessions: sessionmaker[Session],
    operation_id: str,
    cancellation: RunSwitchCancellation,
) -> None:
    """Commit intent before attempting any journal evidence or child observation."""
    with sessions.begin() as session:
        job = session.get(Job, operation_id, with_for_update=True)
        if job is None or job.state not in {
            LifecycleState.QUEUED.value,
            LifecycleState.RUNNING.value,
            LifecycleState.OBSERVING.value,
        }:
            return
        # The repair may have committed after cancel's initial snapshot. The
        # same Job lock hands intent to the now-readable canonical checkpoint.
        if _candidate(job) is None:
            progress = RunSwitchOperationResult.model_validate_json(
                canonical_message(job.result), strict=True
            )
            if progress.cancellation is None:
                progress.cancellation = cancellation
                job.result = progress.model_dump(mode="json")
                job.updated_at = cancellation.requested_at
            return
        row, state = _pending(session, job, cancellation.requested_at)
        if row.cancellation is None:
            row.cancellation = cancellation
        if isinstance(row.cancellation, RunSwitchCancellation):
            state.cancellation = row.cancellation
        state.next_attempt_at = cancellation.requested_at
        row.progress = state
