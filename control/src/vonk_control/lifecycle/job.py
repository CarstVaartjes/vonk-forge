"""The lifecycle adapter of a generic ``Job`` and its ``JobAttempt``.

``Job`` is the base every kind shares: the parent row of a recipe order, a
Run/Switch operation, an upgrade rollout, and the executor jobs of
:class:`~vonk_control.jobs.JobService` (``reconcile``, distribution children,
runtime preflight, recovery stops).  The kinds that have their own adapter
(:mod:`.run_switch`, :mod:`.agent_operation`'s parent aggregate, ...) decide their
own state; this module owns what is generic and is the **only** writer of the
rest:

* :meth:`JobAdapter.new_job` creates a job (``queued`` by default; a coordinator
  that has already issued its first order is created ``running``);
* :meth:`JobAdapter.claim` is the core's ``Claimed`` event: a ``queued`` job, or a
  ``running`` job whose lease lapsed (``LeaseLapsed`` first, so the lapse goes
  through the core, then the claim takes the due retry), gets a new attempt under a
  new fence and lease;
* :meth:`JobAdapter.heartbeat` is the core's ``Heartbeat`` (a stale fence is
  dropped);
* :meth:`JobAdapter.finish` is a definite ``Reported`` (ok, or failed and not
  retryable): the job and its attempt end together;
* :meth:`JobAdapter.resume` is the ``resume`` ``OperatorAction`` of a legacy
  ``waiting-for-operator`` job, applied only when the row advertises it;
* :meth:`JobAdapter.amend_ended` corrects an ended job by evidence (a transfer
  that ended ``succeeded`` while a member failed is ``failed``).

Stored vocabulary is unchanged (``waiting``/``partial``/``expired`` belong to the
owners that write them):

========================  =====================================================
core state                stored job
========================  =====================================================
``queued``/``backoff``    ``queued``
``running``/``observing`` ``running``
``needs-operator``        ``waiting-for-operator``: legacy only
``succeeded``/``failed``  ``succeeded``/``failed``
``cancelled``             ``cancelled``
========================  =====================================================

A generic job is never irreversible: its claim is "claim an expired lease again",
and its handlers are idempotent by contract (an irreversible effect lives in an
agent order, which has its own adapter).  So rule 1 retries it, and rule 3 can
never park it: a legacy ``waiting-for-operator`` job is re-evaluated by the first
decision and retried, and ``resume`` stays advertised for it until then.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..agent_operation_facts import aware
from ..logging import redact_text
from ..models import Job, JobAttempt
from .adapter import Dispatch
from .core import transition
from .reconciler import Settled
from .reconciler import settle as settle_event
from .types import (
    Claimed,
    Effect,
    Event,
    Heartbeat,
    LeaseLapsed,
    Lifecycle,
    Observed,
    OperatorAction,
    Outcome,
    Reported,
    State,
    StopResult,
)

KIND = "job"
WAITING = "waiting-for-operator"
_MAX_REASON = 1024
_STORED = {
    State.QUEUED: "queued",
    State.BACKOFF: "queued",
    State.RUNNING: "running",
    State.OBSERVING: "running",
    State.NEEDS_OPERATOR: WAITING,
    State.SUCCEEDED: "succeeded",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
}
_STATES = {
    "queued": State.QUEUED,
    "running": State.RUNNING,
    WAITING: State.NEEDS_OPERATOR,
    "succeeded": State.SUCCEEDED,
    "failed": State.FAILED,
    "cancelled": State.CANCELLED,
}


class JobAdapter:
    """``KindAdapter`` for a generic stored job; bound to the caller's session."""

    kind = KIND

    def __init__(
        self,
        session: Session | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return aware(self._clock())

    # ------------------------------------------------------------- creation

    @staticmethod
    def new_job(*, state: str = "queued", **fields: Any) -> Job:
        """A new job; the only constructor of a generic ``Job``."""

        return Job(state=state, **fields)

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: Job) -> Lifecycle:
        return self.lifecycle(stored, None)

    def lifecycle(self, job: Job, attempt: JobAttempt | None) -> Lifecycle:
        """Map a stored, possibly legacy, job (and its current attempt) onto the core.

        A ``running`` job keeps its lease (a live one is never touched); a state
        the adapter does not know is ``needs-operator`` and re-evaluated by the
        first decision (rule 3: nothing is irreversible, so it is retried).
        """

        state = _STATES.get(job.state, State.NEEDS_OPERATOR)
        live = attempt is not None and state is State.RUNNING
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        else:
            effect = Effect.ISSUED if job.current_attempt else Effect.NONE
        ordinal = (
            job.payload.get("workload_intent_ordinal")
            if isinstance(job.payload, Mapping)
            else None
        )
        return Lifecycle(
            id=job.id,
            kind=KIND,
            state=state,
            attempt=job.current_attempt,
            fence=attempt.fence if live and attempt is not None else None,
            lease_deadline=(
                aware(attempt.lease_deadline) if live and attempt is not None else None
            ),
            intent_ordinal=ordinal if type(ordinal) is int else None,
            effect=effect,
            reason=job.status_reason,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The executor claims its own work (a polled kind)."""

        return Dispatch(issued=False)

    def observe(self, row: Lifecycle) -> Observed:
        return Observed(Effect.UNKNOWN, "a generic job has no effect to inspect")

    def stop(self, row: Lifecycle) -> StopResult:
        return StopResult.UNCONFIRMED

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """``resume`` (``POST /api/jobs/{id}/resume``) exists for a parked job."""

        return ("resume",) if row.state is State.NEEDS_OPERATOR else ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()

    # ---------------------------------------------------------------- writes

    def _attempt(self, session: Session, job: Job) -> JobAttempt | None:
        if not job.current_attempt:
            return None
        return (
            session.query(JobAttempt)
            .filter_by(job_id=job.id, attempt=job.current_attempt)
            .one_or_none()
        )

    def apply(
        self,
        job: Job,
        after: Lifecycle,
        now: datetime,
        *,
        attempt: JobAttempt | None = None,
        result: Mapping[str, object] | None | object = None,
        reason: str | None | object = None,
    ) -> None:
        """The only writer of ``Job.state`` and ``JobAttempt.state`` of a generic job."""

        stored = _STORED[after.state]
        job.state = stored
        job.updated_at = now
        if after.terminal and attempt is not None:
            attempt.state = stored
        if result is not None:
            job.result = dict(result) if isinstance(result, Mapping) else None
        if reason is not None:
            job.status_reason = (
                redact_text(str(reason))[:_MAX_REASON] if reason else None
            )
        elif after.state in {State.QUEUED, State.BACKOFF} and not after.reason:
            job.status_reason = None

    def settle(
        self,
        session: Session,
        job: Job,
        event: Event,
        now: datetime,
        *,
        attempt: JobAttempt | None = None,
        result: Mapping[str, object] | None = None,
        reason: str | None = None,
        guard: str | None = None,
    ) -> Settled:
        """Feed ``event`` through the core and write what it decides.

        ``guard`` makes the write a compare-and-set on the stored state (an
        operator action races the owners); a lost race is ``stale``.
        """

        row = self.lifecycle(job, attempt or self._attempt(session, job))

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            if guard is not None:
                outcome = session.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.state == guard)
                    .values(state=_STORED[after.state], updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if not isinstance(outcome, CursorResult) or outcome.rowcount != 1:
                    return False
            self.apply(job, after, now, attempt=attempt, result=result, reason=reason)
            return True

        return settle_event(
            row, event, self, now, save=save, residue=lambda _row, _why: None
        )

    # ------------------------------------------------------- claim and lease

    def claim(
        self,
        session: Session,
        job: Job,
        *,
        worker_id: str,
        fence: str,
        lease_deadline: datetime,
        now: datetime,
    ) -> JobAttempt | None:
        """Take ``job``: a new attempt under a new fence; ``None`` when not claimable."""

        previous = self._attempt(session, job)
        row = self.lifecycle(job, previous)
        if row.state is State.RUNNING:
            # The caller selected it because its lease lapsed: the lapse is decided
            # by the core, and the claim is the executor taking the retry it
            # schedules (no operator, no wait beyond the claim itself).
            lapsed = transition(
                row, LeaseLapsed("the attempt lease lapsed"), self, now
            ).row
            row = replace(lapsed, next_action_at=None)
        claimed = transition(
            row, Claimed(row.attempt + 1, fence, lease_deadline), self, now
        ).row
        if claimed.state is not State.RUNNING or claimed.attempt != row.attempt + 1:
            return None
        if previous is not None:
            previous.state = "expired"
        job.current_attempt = claimed.attempt
        job.state = _STORED[State.RUNNING]
        job.updated_at = now
        fresh = JobAttempt(
            job_id=job.id,
            attempt=claimed.attempt,
            fence=fence,
            worker_id=worker_id,
            lease_deadline=lease_deadline,
            state="running",
        )
        session.add(fresh)
        return fresh

    def heartbeat(
        self, job: Job, attempt: JobAttempt, lease_deadline: datetime, now: datetime
    ) -> bool:
        """Extend the lease of the current attempt; a stale fence is dropped."""

        row = self.lifecycle(job, attempt)
        after = transition(row, Heartbeat(attempt.fence, lease_deadline), self, now).row
        if after.lease_deadline != lease_deadline:
            return False
        attempt.lease_deadline = lease_deadline
        job.updated_at = now
        return True

    def finish(
        self,
        job: Job,
        attempt: JobAttempt,
        *,
        succeeded: bool,
        result: Mapping[str, object] | None,
        reason: str | None,
        now: datetime,
    ) -> bool:
        """A definite report from the attempt's executor; ends job and attempt."""

        row = self.lifecycle(job, attempt)
        event = Reported(
            Outcome.OK if succeeded else Outcome.FAILED,
            fence=attempt.fence,
            retryable=False,
            reason=reason,
        )
        after = transition(row, event, self, now).row
        if not after.terminal:
            return False
        self.apply(job, after, now, attempt=attempt, result=result)
        job.result = dict(result) if result is not None else None
        job.status_reason = redact_text(reason)[:_MAX_REASON] if reason else None
        return True

    # --------------------------------------------------------------- operator

    def resume(self, session: Session, job: Job, now: datetime) -> bool:
        """``resume`` of a legacy ``waiting-for-operator`` job; ``False`` if it lost.

        The row advertises ``resume`` only while it is waiting; the state is a
        compare-and-set so a concurrent owner write wins.
        """

        settled = self.settle(
            session,
            job,
            OperatorAction("resume"),
            now,
            guard=WAITING,
            reason="",
        )
        return not settled.stale and settled.changed > 0 and job.state == "queued"

    @staticmethod
    def amend_ended(job: Job, reason: str | None, now: datetime) -> bool:
        """Evidence overrides an optimistic end: ``succeeded`` becomes ``failed``.

        Used by an owner that read more than the aggregate did (a transfer whose
        member failed).  Only that direction is allowed; nothing else is rewritten.
        """

        if job.state != "succeeded":
            return False
        job.state = "failed"
        job.status_reason = reason
        job.updated_at = now
        return True


__all__ = ["KIND", "WAITING", "JobAdapter"]
