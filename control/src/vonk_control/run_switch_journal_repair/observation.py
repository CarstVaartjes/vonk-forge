"""Run switch journal repair: observation."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    UnknownOutcomeError,
    canonical_message,
)

from ..admission_locking import AdmissionLockBusy
from ..models import (
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RunSwitchJournalRepairPending,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
)
from ..run_switch_journal_contract import (
    JournalRepairDisposition,
    JournalRepairPurpose,
)
from ..stall_window import Verdict, Wait, progress_verdict, stall_deadline
from .discovery import REPAIR_WAIT, _candidate
from .ending import _end_unproven_journal
from .pending import REPAIR_BUDGET, _pending
from .repair import _try_repair_once


def try_repair_zero_transfer_journal(
    sessions: sessionmaker[Session],
    operation_id: str,
    now: datetime,
    *,
    purpose: JournalRepairPurpose = JournalRepairPurpose.MEASUREMENT,
) -> JournalRepairDisposition:
    """Observe within the durable clock even when a database boundary fails.

    The clock is never rewritten by a failed read or commit. When the database
    returns, the normal worker either resumes or reconciles the expired owner.
    Database authentication and authorization remain actual refusals.
    """
    try:
        return _observe_zero_transfer_journal(
            sessions, operation_id, now, purpose=purpose
        )
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig, "pgcode", None
        )
        if code is not None and (code.startswith("28") or code == "42501"):
            raise
        return JournalRepairDisposition.DEFERRED


def _observe_zero_transfer_journal(
    sessions: sessionmaker[Session],
    operation_id: str,
    now: datetime,
    *,
    purpose: JournalRepairPurpose,
) -> JournalRepairDisposition:
    from ..agent_operation_facts import aware

    with sessions() as session:
        snapshot = session.get(Job, operation_id)
        if (
            snapshot is None
            or snapshot.state
            not in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
                LifecycleState.OBSERVING.value,
            }
            or _candidate(snapshot) is None
        ):
            return JournalRepairDisposition.NOT_APPLICABLE
    with sessions.begin() as session:
        job = session.scalar(
            select(Job).where(Job.id == operation_id).with_for_update(skip_locked=True)
        )
        if job is None:
            return JournalRepairDisposition.DEFERRED
        if (
            job.state
            not in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
                LifecycleState.OBSERVING.value,
            }
            or _candidate(job) is None
        ):
            return JournalRepairDisposition.NOT_APPLICABLE
        row, state = _pending(session, job, now)
        if now < aware(state.next_attempt_at) and now < aware(state.deadline_at):
            return JournalRepairDisposition.DEFERRED
        cancellation = state.cancellation
        if cancellation is not None:
            purpose = JournalRepairPurpose.CANCELLATION
        candidate = _candidate(job)
        child = candidate.child_operation_id if candidate is not None else None
        expired = (
            progress_verdict(
                started_at=aware(state.deadline_at) - REPAIR_BUDGET,
                progressed_at=None,
                window=REPAIR_BUDGET,
                now=now,
                # A child install whose agent still holds a live attempt owns
                # the missing evidence: it keeps working and is never ended
                # for it. Damaged or abandoned bookkeeping has no live attempt.
                waiting_on=Wait(
                    child is not None
                    and cancellation is None
                    and _child_attempt_is_live(session, child, now)
                ),
            )
            is Verdict.STALLED
        )
        if not expired and now >= aware(state.deadline_at):
            row.deadline_at = stall_deadline(now, None, REPAIR_BUDGET)
            state.deadline_at = row.deadline_at
            row.progress = state
    if expired:
        try:
            return _end_unproven_journal(sessions, operation_id, now)
        except AdmissionLockBusy:
            return JournalRepairDisposition.DEFERRED
    try:
        result = _try_repair_once(sessions, operation_id, now, purpose=purpose)
    except DBAPIError as error:
        # Observation has already committed its immutable clock. Recoverable
        # database access faults consume that same budget, never a new one.
        # Authentication and permission refusals remain the database's answer.
        code = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig, "pgcode", None
        )
        if code is not None and (code.startswith("28") or code == "42501"):
            raise
        result = JournalRepairDisposition.DEFERRED
    except (TypeError, ValueError, UnknownOutcomeError):
        result = JournalRepairDisposition.DEFERRED
    with sessions.begin() as session:
        job = session.scalar(
            select(Job).where(Job.id == operation_id).with_for_update(skip_locked=True)
        )
        if job is None:
            return result
        row = session.get(RunSwitchJournalRepairPending, operation_id)
        if row is None:
            return result
        _, state = _pending(session, job, now)
        if result == JournalRepairDisposition.REPAIRED:
            if state.cancellation is not None:
                progress = RunSwitchOperationResult.model_validate_json(
                    canonical_message(job.result), strict=True
                )
                progress.cancellation = state.cancellation
                job.result = progress.model_dump(mode="json")
            session.delete(row)
        elif result == JournalRepairDisposition.DEFERRED:
            state.attempts += 1
            state.next_attempt_at = min(
                state.deadline_at,
                now + timedelta(seconds=min(2 ** min(state.attempts, 5), 30)),
            )
            row.progress = state
            job.status_reason = f"{REPAIR_WAIT}: exact child evidence unavailable; next attempt {state.next_attempt_at.isoformat()}; deadline {state.deadline_at.isoformat()}"
    return result


def _child_attempt_is_live(session: Session, child_id: str, now: datetime) -> bool:
    from ..agent_operation_facts import attempt_is_live

    for operation in session.scalars(
        select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
    ):
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if attempt_is_live(operation, attempt, now):
            return True
    return False
