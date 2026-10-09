"""Run switch journal repair: ending."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    ReservationState,
)

from .. import agent_operation_states
from ..admission_locking import AdmissionRowLock, lock_admission_rows
from ..job_documents import RecipeInstallParent
from ..models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RunSwitchJournalRepair,
)
from ..run_switch_contract import (
    RunSwitchRuntimePlanResult,
)
from ..run_switch_journal_contract import (
    JournalRepairCode,
    JournalRepairDisposition,
    RunSwitchJournalRepairEndEvidence,
)
from ..stored_json import read_row_column
from .discovery import _candidate, _discover, _fingerprint, journal_document
from .pending import _pending


def _end_unproven_journal(
    sessions: sessionmaker[Session], operation_id: str, now: datetime
) -> JournalRepairDisposition:
    """End bookkeeping, fence only its exact install orders, preserve all bytes."""
    from ..agent_jobs import release_owned_reservations_in_session
    from ..lifecycle.agent_operation import AgentOperationAdapter
    from ..lifecycle.recipe_operation import RecipeOperationAdapter
    from ..lifecycle.run_switch import RunSwitchAdapter
    from ..lifecycle.types import Outcome
    from ..models import RecipeInstallation, ResourceReservation
    from ..run_switch_operations import _run_switch_payload

    # Discover the complete write set without holding any row. Acquire it in
    # the shared order and NOWAIT; never take resource locks under a Job lock.
    with sessions() as session:
        snapshot = session.get(Job, operation_id)
        candidate = _candidate(snapshot) if snapshot is not None else None
        if snapshot is None or candidate is None:
            return JournalRepairDisposition.NOT_APPLICABLE
        captured = _discover(session, operation_id)
        snapshot_fingerprint = _fingerprint(snapshot)
        child_id = candidate.child_operation_id
        installation_id = next(
            (
                receipt.installation_id
                for receipt in reversed(candidate.phase_results)
                if isinstance(receipt, RunSwitchRuntimePlanResult)
            ),
            None,
        )
        owner_ids = [operation_id, installation_id, candidate.profile_application_id]
        requests = [
            AdmissionRowLock(
                "expiry-nodes",
                AgentNode,
                select(AgentNode).where(AgentNode.node_id.in_(snapshot.targets)),
            ),
            AdmissionRowLock(
                "expiry-installation",
                RecipeInstallation,
                select(RecipeInstallation).where(
                    RecipeInstallation.id == installation_id
                ),
            ),
            AdmissionRowLock(
                "expiry-reservations",
                ResourceReservation,
                select(ResourceReservation).where(
                    ResourceReservation.owner_id.in_(owner_ids)
                ),
            ),
            AdmissionRowLock(
                "expiry-jobs",
                Job,
                select(Job).where(Job.id.in_([operation_id, child_id])),
            ),
            AdmissionRowLock(
                "expiry-orders",
                AgentOperation,
                select(AgentOperation).where(AgentOperation.parent_job_id == child_id),
            ),
            AdmissionRowLock(
                "expiry-attempts",
                AgentOperationAttempt,
                select(AgentOperationAttempt)
                .join(
                    AgentOperation,
                    AgentOperation.id == AgentOperationAttempt.operation_id,
                )
                .where(AgentOperation.parent_job_id == child_id),
            ),
        ]
    with sessions.begin() as session:
        lock_admission_rows(session, requests)
        job = session.get(Job, operation_id, populate_existing=True)
        if (
            job is None
            or _fingerprint(job) != snapshot_fingerprint
            or _discover(session, operation_id) != captured
        ):
            return JournalRepairDisposition.DEFERRED
        progress = _candidate(job)
        if progress is None or job.state not in {
            LifecycleState.QUEUED.value,
            LifecycleState.RUNNING.value,
            LifecycleState.OBSERVING.value,
        }:
            return JournalRepairDisposition.NOT_APPLICABLE
        row, state = _pending(session, job, now)
        parent = _run_switch_payload(job)
        child = session.get(Job, progress.child_operation_id)
        # Scope cleanup to the accepted idempotent installation, never a run or
        # a destructive phase. No observation authorizes withdrawing a route.
        child_parent = read_row_column(child, "payload") if child is not None else None
        if (
            child is not None
            and isinstance(child_parent, RecipeInstallParent)
            and parent is not None
            and child.kind == "recipe.install"
            and child.actor == job.actor
            and child_parent.owner_id == installation_id
            and child_parent.workload_intent_ordinal == parent.workload_intent_ordinal
            and set(child.targets) == set(job.targets)
        ):
            for operation in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == child.id)
            ):
                if operation.state in agent_operation_states.LIVE:
                    attempt = session.scalar(
                        select(AgentOperationAttempt).where(
                            AgentOperationAttempt.operation_id == operation.id,
                            AgentOperationAttempt.attempt == operation.current_attempt,
                        )
                    )
                    AgentOperationAdapter(session).record_outcome(
                        operation,
                        attempt,
                        child,
                        Outcome.CANCELLED,
                        now,
                        reason=JournalRepairCode.EXHAUSTED,
                    )
                    AgentOperationAdapter.withdraw_retry(operation)
                for retained_attempt in session.scalars(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == operation.id
                    )
                ):
                    AgentOperationAdapter.end_unobserved_attempt(retained_attempt, now)
            if child.state not in {
                LifecycleState.SUCCEEDED.value,
                LifecycleState.FAILED.value,
                LifecycleState.CANCELLED.value,
            }:
                RecipeOperationAdapter().cancelled(
                    child, now, reason=JournalRepairCode.EXHAUSTED
                )
            release_owned_reservations_in_session(
                session, child_parent.owner_kind, child_parent.owner_id, now
            )
        # A missing child row is not evidence that its installation's disk
        # promise is still needed. Actual stored bytes remain in capacity facts;
        # run-owned memory/port claims and routes are independent and untouched.
        if child is None and installation_id is not None:
            release_owned_reservations_in_session(
                session, "installation", installation_id, now
            )
        if progress.profile_application_id is not None:
            # Only promises still held by this preparation application. Running
            # assignment claims have already transferred to their run owners.
            for reservation in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == progress.profile_application_id,
                    ResourceReservation.node_id.in_(job.targets),
                    ResourceReservation.state.in_(
                        [ReservationState.ACTIVE.value, ReservationState.PROMISED.value]
                    ),
                )
            ):
                reservation.state = ReservationState.RELEASED.value
                reservation.released_at = now
        # Parent reservations, if any, cannot survive its end.
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == job.id,
                ResourceReservation.state.in_(
                    [ReservationState.ACTIVE.value, ReservationState.PROMISED.value]
                ),
            )
        ):
            reservation.state = ReservationState.RELEASED.value
            reservation.released_at = now
        reason = f"{JournalRepairCode.EXHAUSTED}: bounded journal observation ended; original evidence retained, installation bytes remain reusable"
        if state.cancellation is not None:
            RunSwitchAdapter().cancelled(job, progress, now, reason=reason)
        else:
            RunSwitchAdapter().reject(job, reason, now)
        # The immutable history retains cancellation after the transient clock
        # is removed. No missing witness is manufactured into repair evidence.
        original = journal_document(job.result)
        original_digest = hashlib.sha256(original.encode()).hexdigest()
        evidence = RunSwitchJournalRepairEndEvidence(
            code=JournalRepairCode.EXHAUSTED.value,
            operation_id=job.id,
            request_key=job.request_id,
            original_digest=original_digest,
            original_document=original,
            cancellation=state.cancellation,
            recorded_at=now,
        )
        session.add(
            RunSwitchJournalRepair(
                job_id=job.id,
                original_digest=original_digest,
                record_kind="end",
                evidence=evidence,
                created_at=now,
            )
        )
        # Do not replace or bless the damaged measurement at expiry.
        session.delete(row)
    return JournalRepairDisposition.ENDED
