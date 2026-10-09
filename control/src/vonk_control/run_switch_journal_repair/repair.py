"""Run switch journal repair: repair."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    UnknownOutcomeError,
    is_state,
)

from .. import agent_operation_states
from ..admission_locking import AdmissionLockBusy
from ..models import (
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RunSwitchJournalRepair,
    RunSwitchJournalRepairPending,
)
from ..run_switch_journal_contract import (
    JournalRepairDisposition,
    JournalRepairPurpose,
    RunSwitchJournalRepairEvidence,
)
from .discovery import (
    REPAIR_WAIT,
    _candidate,
    _discover,
    _lock_discovery,
    _plan_digest,
    journal_document,
)
from .pending import _pending
from .proof import _prove


def _try_repair_once(
    sessions: sessionmaker[Session],
    operation_id: str,
    now: datetime,
    *,
    purpose: JournalRepairPurpose = JournalRepairPurpose.MEASUREMENT,
) -> JournalRepairDisposition:
    """One short conditional repair transaction; all native reads stay in SQL."""
    # Normal healthy jobs pay one indexed read and no additional row lock.
    with sessions() as session:
        snapshot = session.get(Job, operation_id)
        if snapshot is None or _candidate(snapshot) is None:
            return JournalRepairDisposition.NOT_APPLICABLE
        captured = _discover(session, operation_id)
        if captured is None:
            return JournalRepairDisposition.DEFERRED
    try:
        with sessions.begin() as session:
            _lock_discovery(session, captured)
            if _discover(session, operation_id) != captured:
                return JournalRepairDisposition.DEFERRED
            job = session.get(Job, operation_id)
            if (
                job is None
                or job.kind != "recipe.run-switch.v2"
                or job.state
                not in {
                    LifecycleState.QUEUED.value,
                    LifecycleState.RUNNING.value,
                    LifecycleState.OBSERVING.value,
                }
            ):
                return JournalRepairDisposition.NOT_APPLICABLE
            progress = _candidate(job)
            if progress is None:
                return JournalRepairDisposition.NOT_APPLICABLE
            if purpose == JournalRepairPurpose.MEASUREMENT:
                child = session.get(Job, progress.child_operation_id)
                attempts = tuple(
                    session.execute(
                        select(AgentOperation, AgentOperationAttempt)
                        .join(
                            AgentOperationAttempt,
                            AgentOperationAttempt.operation_id == AgentOperation.id,
                        )
                        .where(
                            AgentOperation.id.in_(captured.native_ids),
                            AgentOperationAttempt.id.in_(captured.attempt_ids),
                            AgentOperationAttempt.attempt
                            == AgentOperation.current_attempt,
                        )
                    )
                )
                expired = any(
                    now
                    >= (
                        attempt.lease_deadline
                        if attempt.lease_deadline.tzinfo
                        else attempt.lease_deadline.replace(tzinfo=now.tzinfo)
                    )
                    for operation, attempt in attempts
                    if operation.state in agent_operation_states.LIVE
                    and (
                        is_state(
                            LifecycleSubject.AGENT_OPERATION_ATTEMPT,
                            attempt.state,
                            LifecycleState.RUNNING,
                        )
                        or agent_operation_states.attempt_lapsed(attempt)
                    )
                )
                deadline = progress.observation_deadline_at
                deadline_expired = deadline is not None and now >= (
                    deadline if deadline.tzinfo else deadline.replace(tzinfo=now.tzinfo)
                )
                if progress.cancellation is not None:
                    purpose = JournalRepairPurpose.CANCELLATION
                elif (
                    (
                        child is not None
                        and child.state
                        in {
                            LifecycleState.SUCCEEDED.value,
                            LifecycleState.FAILED.value,
                            LifecycleState.CANCELLED.value,
                        }
                    )
                    or expired
                    or deadline_expired
                ):
                    purpose = JournalRepairPurpose.OWNER_OBSERVATION
            due = progress.observation_due_at
            if (
                purpose == JournalRepairPurpose.MEASUREMENT
                and due is not None
                and now
                < (due if due.tzinfo is not None else due.replace(tzinfo=now.tzinfo))
            ):
                return JournalRepairDisposition.DEFERRED
            try:
                witnesses = _prove(session, job, progress, now, purpose)
            except (KeyError, TypeError, ValueError, UnknownOutcomeError):
                witnesses = None
            if witnesses is None:
                job.status_reason = f"{REPAIR_WAIT}: waiting for exact accepted zero-transfer/native sample evidence; existing observation deadline remains unchanged"
                return JournalRepairDisposition.DEFERRED
            original = journal_document(job.result)
            assert job.result is not None
            corrected = dict(job.result)
            corrected["completed_bytes"] = 0
            original_digest = hashlib.sha256(original.encode()).hexdigest()
            evidence = RunSwitchJournalRepairEvidence(
                algorithm="zero-transfer-native-install-v1",
                purpose=purpose,
                operation_id=job.id,
                request_key=job.request_id,
                payload_digest=job.payload_digest,
                plan_digest=_plan_digest(job),
                original_digest=original_digest,
                corrected_digest=hashlib.sha256(
                    journal_document(corrected).encode()
                ).hexdigest(),
                original_document=original,
                native_samples=witnesses,
                recorded_at=now,
            )
            # The Job lock prevents concurrent checkpoint mutation; unique key
            # makes recovery idempotent without a second live state document.
            previous = session.scalar(
                select(RunSwitchJournalRepair).where(
                    RunSwitchJournalRepair.job_id == job.id,
                    RunSwitchJournalRepair.original_digest == original_digest,
                    RunSwitchJournalRepair.record_kind == "repair",
                )
            )
            if previous is None:
                session.add(
                    RunSwitchJournalRepair(
                        job_id=job.id,
                        original_digest=original_digest,
                        evidence=evidence,
                        created_at=now,
                    )
                )
            # Historical diagnostics never veto current verified witnesses.
            # An existing row may be unreadable or describe an earlier sample;
            # neither changes the accepted request or the proof above.
            pending = session.get(RunSwitchJournalRepairPending, operation_id)
            if pending is not None:
                _, retained_state = _pending(session, job, now)
                if retained_state.cancellation is not None:
                    corrected["cancellation"] = retained_state.cancellation.model_dump(
                        mode="json"
                    )
                session.delete(pending)
            job.result = corrected
            job.updated_at = now
            if (job.status_reason or "").startswith(REPAIR_WAIT):
                job.status_reason = None
            return JournalRepairDisposition.REPAIRED
    except AdmissionLockBusy:
        return JournalRepairDisposition.DEFERRED
