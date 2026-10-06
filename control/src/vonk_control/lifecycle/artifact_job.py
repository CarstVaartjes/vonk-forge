"""The lifecycle adapter of the one-shot artifact job (``ArtifactJob``).

An artifact job has no attempt, lease or retry of its own: it is the user-facing
projection of its ``recipe.job.run.v1`` order (the one :class:`AgentOperation`
under the job's ``operation_id``).  The order is decided by the core through
:class:`~vonk_control.lifecycle.agent_operation.AgentOperationAdapter`; this module
owns the *projection* and the only writer of an artifact job's ``state``:

* :meth:`ArtifactJobAdapter.lifecycle` (the ``adopt`` hook) maps a stored, possibly
  legacy, artifact job onto the core, taking attempt, fence, lease, observation
  count and the cancel request from the order;
* :meth:`ArtifactJobAdapter.project` copies what the order decided onto the job, so
  there is no second, duplicated state to drift (a cancelled order that ended
  after its stop budget ends its job; a legacy ``waiting-for-operator`` job
  follows its order);
* :meth:`ArtifactJobAdapter.settle` feeds an owner's own event (a validated agent
  result, a cancel of work that never ran) through the core's ``transition`` and
  writes what it decides.

An artifact job has a *preparation* stage before it is submitted (``draft`` while
its inputs upload, ``ready`` once complete; the lifecycle ``state`` is ``NULL``) and,
from submit, a lifecycle ``state`` of the core vocabulary stored as itself.  A cancel
is the monotonic ``cancel_requested_at``; it never changes the state.  Rows written
before the rename kept this in one word (``draft``, ``ready``, ``cancelling``,
``waiting-for-operator``); ``artifact_job_states`` reads either shape and the startup
adoption rewrites them.

========================  ==========================================
core state of the order   stored artifact job ``state``
========================  ==========================================
``queued``                ``queued`` (``NULL`` before submit)
``running``               ``running``
``backoff``/``observing`` ``backoff``/``observing``
``needs-operator``        ``needs-operator``
``succeeded``             ``succeeded``
``failed``                ``failed``
``cancelled``             ``cancelled``
========================  ==========================================

A one-shot job is irreversible: its process may have run.  So its only operator
action is ``stop``, advertised exactly while the job is in doubt and no cancel has
been requested (:meth:`ArtifactJobAdapter.actions`).  A cancel always completes:
when the stop cannot be confirmed the order ends ``cancelled`` with the effect
unknown (core rule 4), and the job ends ``cancelled`` with a residue record in
its result evidence (``active_scope_may_remain``) that the run's Stop and the
retention sweep read.  A job never waits for an operator without that action.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState, legacy_preparation

from .. import agent_operation_states as aos
from .. import artifact_job_states as ajs
from ..agent_operation_facts import aware
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, ArtifactJob, Job
from .adapter import Dispatch
from .agent_operation import (
    JOB_RUN_OPERATION,
    OWNER_KIND,
    STOP_ACTION,
    AgentOperationAdapter,
    is_artifact_owned,
)
from .core import transition
from .reconciler import settle
from .types import (
    Effect,
    Event,
    Lifecycle,
    Observed,
    Outcome,
    Reported,
    State,
    StopResult,
)

KIND = "artifact-job"
WAITING = ajs.NEEDS_OPERATOR
TERMINAL_STATES = frozenset(ajs.ENDED)
#: Stored states of a submitted job that still depends on its order.
LIVE_STATES = frozenset(ajs.LIVE)
_MAX_REASON = 512
_RECEIPT_STATES = {"cancelled": Effect.STOPPED, "succeeded": Effect.ESTABLISHED}
_CANCELLING_REASON = "waiting for exact artifact cancellation receipt"
_OBSERVING_REASON = "the agent can no longer report on the job; observing it"
_WAITING_REASON = (
    "the job's effect is unknown; Stop it to cancel the job and record the "
    "possible residue"
)
_RESIDUE_REASON = (
    "cancelled; the stop could not be confirmed, so the job's scope may remain"
)


class ArtifactJobAdapter:
    """``KindAdapter`` for a stored artifact job, bound to a session or a factory."""

    kind = KIND

    def __init__(
        self,
        session: Session | None = None,
        *,
        sessions: sessionmaker[Session] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (session is None) == (sessions is None):
            raise ValueError("bind the adapter to a session or to a session factory")
        self._session = session
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._orders = AgentOperationAdapter(session, sessions=sessions, clock=clock)

    # ------------------------------------------------------------- plumbing

    @property
    def session(self) -> Session:
        """The session this adapter is bound to (inline use only)."""

        assert self._session is not None
        return self._session

    @contextmanager
    def _read(self):
        if self._session is not None:
            yield self._session
        else:
            assert self._sessions is not None
            with self._sessions() as session:
                yield session

    def now(self) -> datetime:
        return aware(self._clock())

    def order_of(
        self, session: Session, job: ArtifactJob
    ) -> tuple[Job | None, StoredOperation | None, AgentOperationAttempt | None]:
        """The submission job, its one order and the order's current attempt."""

        if job.operation_id is None:
            return None, None, None
        parent = session.get(Job, job.operation_id)
        operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.parent_job_id == job.operation_id)
            .order_by(StoredOperation.id)
            .limit(1)
        )
        attempt = (
            None
            if operation is None
            else AgentOperationAdapter.attempt_of(session, operation)
        )
        return parent, operation, attempt

    # ------------------------------------------------------- pre-submission

    @staticmethod
    def new_job(**fields: Any) -> ArtifactJob:
        """Create a job: it is born preparing (``draft``), with no state and no order."""

        return ArtifactJob(preparation=ajs.DRAFT, state=None, **fields)

    @staticmethod
    def mark_ready(job: ArtifactJob, now: datetime) -> None:
        """Its inputs are complete: ``draft`` becomes ``ready``."""

        job.preparation = ajs.READY
        job.finalized_at = aware(now)
        job.updated_at = aware(now)

    @staticmethod
    def mark_submitted(job: ArtifactJob, operation_id: str, now: datetime) -> None:
        """Its order was queued: the job is ``queued`` and bound to the order."""

        job.operation_id = operation_id
        job.preparation = None
        job.state = ajs.QUEUED
        job.submitted_at = aware(now)
        job.updated_at = aware(now)

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: ArtifactJob) -> Lifecycle:
        """Map a stored (possibly legacy) job onto the core; pure but for reads."""

        with self._read() as session:
            parent, operation, attempt = self.order_of(session, stored)
            return self.lifecycle(stored, parent, operation, attempt, self.now())

    def lifecycle(
        self,
        job: ArtifactJob,
        parent: Job | None,
        operation: StoredOperation | None,
        attempt: AgentOperationAttempt | None,
        now: datetime,
    ) -> Lifecycle:
        """The lifecycle row of the stored job, defaulting what a legacy row lacks.

        Attempt, fence, lease, observation count and the cancel request come from
        the order (the job has none of its own); the state is the job's stored one.
        """

        now = aware(now)
        order = (
            None
            if operation is None
            else self._orders.lifecycle(operation, attempt, parent, now)
        )
        stored = ajs.state_of(job)
        cancelled_at = ajs.cancel_requested_at(job)
        if stored == ajs.RUNNING:
            state = State.RUNNING
        elif stored == ajs.OBSERVING:
            state = State.OBSERVING
        elif stored == LifecycleState.BACKOFF.value:
            state = State.BACKOFF
        elif stored == WAITING:
            state = State.NEEDS_OPERATOR
        elif stored == "succeeded":
            state = State.SUCCEEDED
        elif stored == "failed":
            state = State.FAILED
        elif stored == "cancelled":
            state = State.CANCELLED
        elif stored == "queued" and order is not None and order.state is State.RUNNING:
            state = State.RUNNING  # the Spark claims its own order
        else:  # draft, ready, queued, or a state this adapter does not know
            state = State.QUEUED
        attempts = 0 if operation is None else operation.current_attempt
        row = Lifecycle(
            id=job.id,
            kind=KIND,
            state=state,
            attempt=attempts,
            fence=None if order is None else order.fence,
            lease_deadline=None if order is None else order.lease_deadline,
            next_action_at=None if order is None else order.next_action_at,
            observe_count=0 if order is None else order.observe_count,
            intent_ordinal=None if order is None else order.intent_ordinal,
            cancel_requested_at=(
                order.cancel_requested_at if order is not None else None
            )
            or (None if cancelled_at is None else aware(cancelled_at)),
            cancel_request_key=None if order is None else order.cancel_request_key,
            effect=Effect.NONE,
            reason=job.status_reason,
            owner_id=job.run_id,
        )
        return _with_effect(row, self._effect(job, state, attempts, attempt, row))

    @staticmethod
    def _effect(
        job: ArtifactJob,
        state: State,
        attempts: int,
        attempt: AgentOperationAttempt | None,
        row: Lifecycle,
    ) -> Effect:
        if state is State.SUCCEEDED:
            return Effect.ESTABLISHED
        if attempts == 0:
            return Effect.NONE
        evidence = job.result_evidence
        residue = isinstance(evidence, Mapping) and (
            evidence.get("active_scope_may_remain") is True
        )
        if state is State.CANCELLED:
            return Effect.UNKNOWN if residue else Effect.STOPPED
        if state is State.FAILED:
            return Effect.UNKNOWN if residue else Effect.NONE
        if state is State.RUNNING and (row.lease_deadline is not None):
            return Effect.ISSUED
        return Effect.UNKNOWN

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """A user's job may have run: repeating it could repeat a visible effect."""

        return True

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The Spark claims its own order; the job issues nothing itself."""

        return Dispatch(issued=False)

    def observe(self, row: Lifecycle) -> Observed:
        """Read the order's receipt: the only evidence of a job's effect."""

        effect = self._receipt(row.id)
        if effect is None:
            return Observed(Effect.UNKNOWN, "the job's receipt has not arrived")
        return Observed(effect)

    def stop(self, row: Lifecycle) -> StopResult:
        """Confirmed when nothing was issued or the agent receipted a cancel."""

        if row.attempt == 0:
            return StopResult.CONFIRMED
        effect = self._receipt(row.id)
        return (
            StopResult.CONFIRMED if effect is Effect.STOPPED else StopResult.UNCONFIRMED
        )

    def _receipt(self, job_id: str) -> Effect | None:
        with self._read() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                return Effect.NONE
            _parent, operation, attempt = self.order_of(session, job)
            if operation is None or operation.current_attempt == 0:
                return Effect.NONE
            if attempt is None:
                return None
            return _RECEIPT_STATES.get(attempt.state)

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """``stop``, exactly while the job is in doubt and no cancel is requested.

        Once a cancel is requested the job is not waiting for anyone: it completes
        by itself (rule 4), so there is nothing left to advertise.
        """

        if (
            row.terminal
            or row.cancel_requested
            or row.attempt == 0
            or row.state is State.QUEUED
        ):
            return ()
        return (STOP_ACTION,)

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()

    def view(
        self, job: ArtifactJob
    ) -> tuple[str | None, tuple[str, ...], datetime | None]:
        """The state, operator actions and cancel request an API reader sees for a job.

        The state is the stored one, read through the order: a claimed ``queued``
        job is ``running`` (the Spark claims its own order), and a legacy
        ``waiting-for-operator`` job whose cancel was already requested is
        ``cancelling`` (it completes by itself; nobody has to act).  Actions are
        derived by :meth:`actions`, never stored.
        """

        with self._read() as session:
            parent, operation, attempt = self.order_of(session, job)
            row = self.lifecycle(job, parent, operation, attempt, self.now())
        state = ajs.state_of(job)
        if state == ajs.QUEUED and row.state is State.RUNNING:
            state = ajs.RUNNING
        elif state == WAITING and row.cancel_requested:
            # It completes by itself; nobody has to act, so it is not a wait.
            state = ajs.OBSERVING
        return state, self.actions(row), row.cancel_requested_at

    def supported_actions(self, job: ArtifactJob) -> tuple[str, ...]:
        """The operator actions of a stored job (what the API advertises)."""

        return self.view(job)[1]

    # ---------------------------------------------------------------- apply

    def apply(
        self,
        job: ArtifactJob,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
        *,
        reason: str | None = None,
        evidence: Mapping[str, object] | None = None,
        output_manifest_sha256: str | None = None,
    ) -> bool:
        """Project a core row onto the stored job; the only writer of its state.

        Returns whether anything changed.  A job already ended absorbs nothing,
        and a row that would be rewritten with the same values is left alone.
        """

        now = aware(now)
        if ajs.is_ended(job):
            return False
        if job.preparation is None and (stage := legacy_preparation(job.state)):
            job.preparation = stage.value  # a row written before the rename
        target = self._stored_state(job, after)
        changed_state = ajs.state_of(job) != target or job.state != target
        text = reason
        if text is None and changed_state:
            text = self._default_reason(after, target)
        if target == "succeeded":
            text = None
        merged = self._evidence(job, after, target, evidence)
        terminal = target in TERMINAL_STATES
        changed = False
        if changed_state:
            job.state = target
            if target is not None:
                job.preparation = None
            changed = True
        if after.cancel_requested and job.cancel_requested_at is None:
            job.cancel_requested_at = after.cancel_requested_at
            changed = True
        if text is not None:
            text = text[:_MAX_REASON]
        if (text is not None or target == "succeeded") and job.status_reason != text:
            job.status_reason = text
            changed = True
        if merged is not None and job.result_evidence != merged:
            job.result_evidence = merged
            changed = True
        if (
            output_manifest_sha256 is not None
            and job.output_manifest_sha256 != output_manifest_sha256
        ):
            job.output_manifest_sha256 = output_manifest_sha256
            changed = True
        if terminal and job.completed_at is None:
            job.completed_at = now
            changed = True
        elif not terminal and job.completed_at is not None:
            job.completed_at = None
            changed = True
        if changed:
            job.updated_at = now
        return changed

    @staticmethod
    def _stored_state(job: ArtifactJob, after: Lifecycle) -> str | None:
        """The core state, stored as itself; ``None`` while the job is preparing."""

        match after.state:
            case State.SUCCEEDED:
                return ajs.SUCCEEDED
            case State.FAILED:
                return ajs.FAILED
            case State.CANCELLED:
                return ajs.CANCELLED
            case State.QUEUED if job.operation_id is None:
                return None
            case State.QUEUED:
                return ajs.QUEUED
            case State.NEEDS_OPERATOR:
                if after.cancel_requested:
                    return ajs.OBSERVING
                # Work that never ran has nothing to stop: it is queued again.
                return WAITING if after.attempt > 0 else ajs.QUEUED
            case State.OBSERVING:
                return ajs.OBSERVING
            case State.BACKOFF:
                return LifecycleState.BACKOFF.value
        return ajs.RUNNING

    @staticmethod
    def _default_reason(after: Lifecycle, target: str | None) -> str | None:
        if after.cancel_requested and not after.terminal:
            return _CANCELLING_REASON
        if after.state in {State.OBSERVING, State.BACKOFF}:
            return _OBSERVING_REASON
        if target == WAITING:
            return _WAITING_REASON
        if target == "cancelled":
            if after.effect is Effect.UNKNOWN and after.attempt > 0:
                return _RESIDUE_REASON
            return after.reason or "artifact job cancelled"
        if target == "failed":
            return after.reason or "recipe job failed"
        return None

    @staticmethod
    def _evidence(
        job: ArtifactJob,
        after: Lifecycle,
        target: str | None,
        given: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        """The result evidence after this change: the old, the report's, the residue.

        A job whose effect is in doubt (after an attempt that ran) records what a
        later stop, the run's Stop and the retention sweep need to know.  The
        keys an owner supplied win over the defaults.
        """

        current = job.result_evidence
        merged: dict[str, object] = (
            dict(current) if isinstance(current, Mapping) else {}
        )
        if given:
            merged.update(given)
        doubtful = (
            after.attempt > 0
            and after.effect is Effect.UNKNOWN
            and target is not None
            and target not in {ajs.SUCCEEDED, ajs.FAILED, ajs.QUEUED}
        )
        if doubtful:
            cancel = after.cancel_requested or target == ajs.CANCELLED
            merged.setdefault(
                "failure_kind",
                "cancellation-stop-uncertain" if cancel else "agent-lease-expired",
            )
            merged.setdefault("recoverable", True)
            merged.setdefault("active_scope_may_remain", True)
            if not cancel:
                merged.setdefault("late_results_accepted", False)
        if not merged:
            return None
        return merged

    # ----------------------------------------------------- core and project

    def settle(
        self,
        job: ArtifactJob,
        event: Event,
        now: datetime,
        *,
        reason: str | None = None,
        evidence: Mapping[str, object] | None = None,
        output_manifest_sha256: str | None = None,
    ) -> Lifecycle:
        """Feed an owner's ``event`` to the core and write what it decides, inline.

        The caller holds the job's lock; ``job`` is changed in place.  An event the
        job absorbs (it already ended) writes nothing.
        """

        now = aware(now)
        session = self._session
        assert session is not None
        parent, operation, attempt = self.order_of(session, job)
        row = self.lifecycle(job, parent, operation, attempt, now)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            self.apply(
                job,
                before,
                after,
                now,
                reason=reason,
                evidence=evidence,
                output_manifest_sha256=output_manifest_sha256,
            )
            return True

        def residue(_row: Lifecycle, _reason: str) -> None:
            return None  # the residue is the evidence ``apply`` merges

        return settle(row, event, self, now, save=save, residue=residue).row

    def confirm_stopped(self, job: ArtifactJob, reason: str, now: datetime) -> bool:
        """An exact Stop receipt proves the job's runtime absent: it ends cancelled.

        A job still live ends ``cancelled`` with the effect stopped (a definite
        report).  A job that ended with its stop unconfirmed (the residue record in
        its evidence) is *resolved* by the receipt: it becomes ``cancelled`` and the
        residue is closed, so nothing waits for it.  An ended job without residue
        keeps its own outcome.
        """

        now = aware(now)
        if not ajs.is_ended(job):
            self.settle(
                job,
                Reported(Outcome.CANCELLED, effect=Effect.STOPPED, reason=reason),
                now,
                reason=reason,
            )
            return True
        evidence = job.result_evidence
        if not (
            isinstance(evidence, Mapping)
            and evidence.get("active_scope_may_remain") is True
        ):
            return False
        job.state = ajs.CANCELLED
        job.status_reason = reason[:_MAX_REASON]
        job.result_evidence = {
            **evidence,
            "active_scope_may_remain": False,
            "residue_resolved_by": "exact-stop",
        }
        job.completed_at = job.completed_at or now
        job.updated_at = now
        return True

    def project(
        self,
        job: ArtifactJob,
        now: datetime,
        *,
        reason: str | None = None,
        evidence: Mapping[str, object] | None = None,
    ) -> bool:
        """Copy what the order decided onto the job; idempotent.

        Returns whether the job changed.  An order that has not reached a state
        the job mirrors (it succeeded, which only the result consumer may judge)
        leaves the job alone.
        """

        now = aware(now)
        session = self._session
        assert session is not None
        if ajs.is_ended(job) or job.operation_id is None:
            return False
        parent, operation, attempt = self.order_of(session, job)
        if operation is None:
            return False
        before = self.lifecycle(job, parent, operation, attempt, now)
        order = self._orders.lifecycle(operation, attempt, parent, now)
        if order.state is State.SUCCEEDED:
            return False
        after = _with_effect(
            Lifecycle(
                id=job.id,
                kind=KIND,
                state=order.state,
                attempt=order.attempt,
                fence=order.fence,
                lease_deadline=order.lease_deadline,
                next_action_at=order.next_action_at,
                observe_count=order.observe_count,
                intent_ordinal=order.intent_ordinal,
                cancel_requested_at=order.cancel_requested_at,
                cancel_request_key=order.cancel_request_key,
                effect=Effect.NONE,
                reason=order.reason,
                owner_id=job.run_id,
            ),
            self._ended_effect(order, attempt),
        )
        return self.apply(job, before, after, now, reason=reason, evidence=evidence)

    @staticmethod
    def _ended_effect(
        order: Lifecycle, attempt: AgentOperationAttempt | None
    ) -> Effect:
        """What is known of the job's effect, from the order and its receipt.

        The order's own row says ``stopped`` for any cancelled order, but only an
        attempt that receipted its cancel proves the job stopped: a cancel that
        ended after its stop budget leaves the effect unknown.
        """

        if order.attempt == 0:
            return Effect.NONE
        if order.state is State.CANCELLED:
            receipt = None if attempt is None else _RECEIPT_STATES.get(attempt.state)
            return Effect.STOPPED if receipt is Effect.STOPPED else Effect.UNKNOWN
        if order.state is State.FAILED:
            return (
                Effect.NONE
                if attempt is not None and attempt.state == "failed"
                else Effect.UNKNOWN
            )
        if order.state is State.RUNNING and order.lease_deadline is not None:
            return Effect.ISSUED
        return Effect.UNKNOWN

    def project_for_order(self, operation: StoredOperation, now: datetime) -> bool:
        """Project the job of an order that just changed (``False`` if it has none)."""

        session = self._session
        assert session is not None
        if operation.kind != JOB_RUN_OPERATION:
            return False
        job = session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.operation_id == operation.parent_job_id)
            .with_for_update(of=ArtifactJob)
        )
        return False if job is None else self.project(job, now)

    # ------------------------------------------------------------- reconcile

    def reconcile(self, limit: int = 100) -> int:
        """Heal every job whose order moved without it (adoption and restarts).

        Selects the live jobs whose order has reached a state the job does not yet
        mirror (an ended or waiting order, a running order under a queued job) and
        projects each in its own short transaction.  Idempotent: a pass interrupted
        anywhere is repeated, and a job another transaction changed is re-read.
        """

        assert self._sessions is not None
        with self._sessions() as session:
            ids = tuple(
                session.scalars(
                    select(ArtifactJob.id)
                    .join(
                        StoredOperation,
                        StoredOperation.parent_job_id == ArtifactJob.operation_id,
                    )
                    .where(
                        ArtifactJob.state.in_(LIVE_STATES),
                        or_(
                            StoredOperation.state.in_(
                                {*aos.PARKED, "cancelled", "failed"}
                            ),
                            (ArtifactJob.state == ajs.QUEUED)
                            & (StoredOperation.state == "running"),
                        ),
                    )
                    .order_by(ArtifactJob.updated_at, ArtifactJob.id)
                    .limit(limit)
                )
            )
        changed = 0
        now = self.now()
        for job_id in ids:
            with self._sessions.begin() as session:
                job = session.scalar(
                    select(ArtifactJob)
                    .where(ArtifactJob.id == job_id)
                    .with_for_update(of=ArtifactJob)
                    .execution_options(populate_existing=True)
                )
                if job is None:
                    continue
                bound = ArtifactJobAdapter(session, clock=self._clock)
                if bound.project(job, now):
                    changed += 1
        return changed


def _with_effect(row: Lifecycle, effect: Effect) -> Lifecycle:
    return replace(row, effect=effect)


def transition_of(
    row: Lifecycle, event: Event, adapter: ArtifactJobAdapter, now: datetime
):
    """The core's decision for ``event`` without writing it (tests and probes)."""

    return transition(row, event, adapter, aware(now))


__all__ = [
    "JOB_RUN_OPERATION",
    "KIND",
    "LIVE_STATES",
    "OWNER_KIND",
    "STOP_ACTION",
    "TERMINAL_STATES",
    "ArtifactJobAdapter",
    "is_artifact_owned",
    "transition_of",
]
