"""The lifecycle adapter of a Run/Switch operation (``Job`` of kind ``recipe.run-switch.v2``).

A Run/Switch operation is a **composite**: it has no effect of its own, its phases
are done by its current child (a recipe job and the Spark orders under it, decided
by :class:`~vonk_control.lifecycle.agent_operation.AgentOperationAdapter`), so it
is never irreversible and never executes anything itself.  ``run_switch_operations``
keeps what is specific to it (the plan, the phase executor, evidence merging,
admission); this module owns the rest, and it is the **only** writer of the
operation's ``Job.state`` and of its retry and observation clock:

* :meth:`RunSwitchAdapter.lifecycle` (the ``adopt`` hook) maps a stored, possibly
  legacy, operation onto the core.  The clock already lives in the stored progress
  (``observation_due_at`` is ``next_action_at``, ``retry_attempt`` is the retry or
  observation count, ``cancellation`` is the monotonic cancel request), so no
  column is added and nothing is rewritten until the row's next decision;
* :meth:`RunSwitchAdapter.settle` feeds an event (a retryable failure, a definite
  end, a cancel request, a tick) through the core's ``transition`` and writes what
  it decides with :meth:`RunSwitchAdapter.apply`;
* :meth:`RunSwitchAdapter.children` and :meth:`RunSwitchAdapter.observe` read the
  child's orders, so the operation follows what the children are.

Stored vocabulary is unchanged (the API, the CLI, the profile application and the
generated clients read it):

========================  =====================================================
core state                stored operation
========================  =====================================================
``queued``                ``queued``
``running``               ``running``
``backoff``               ``running`` (a phase retry) or ``waiting`` (a re-plan or
                          an inactive Spark), with ``observation_due_at``
``observing``             ``waiting`` (or ``running`` while a child is observed)
``needs-operator``        ``waiting-for-operator``: legacy only, re-evaluated and
                          never written (no Run/Switch action exists)
``succeeded``/``failed``  ``succeeded``/``failed``
``cancelled``             ``cancelled``
========================  =====================================================

Why a Run/Switch never waits for an operator: it has no ``resume``/``retire``
action, its children own their effects, and everything it can be unsure about (a
receipt that does not validate, a verification it cannot observe, a child it cannot
find) is observed again by re-entering the same idempotent phase.  A cancel always
completes: the child is stopped and observed up to the core's stop budget, then the
operation ends ``cancelled`` with the child's effect unknown (a residue note in its
reason); a child that is left running keeps its own lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..agent_operation_facts import aware
from ..models import AgentOperation as StoredOperation
from ..models import Job
from .adapter import Dispatch
from .agent_operation import AgentOperationAdapter
from .composite import aggregate
from .core import transition
from .reconciler import Settled
from .reconciler import settle as settle_event
from .types import (
    TERMINAL_STATES,
    CancelRequested,
    Decision,
    Effect,
    Event,
    Lifecycle,
    Observed,
    Outcome,
    Reported,
    State,
    StopResult,
    Tick,
)

KIND = "run-switch"
WAITING = "waiting-for-operator"
_MAX_REASON = 512
#: Stored states an operation can still advance from.
LIVE_STATES = frozenset({"queued", "running", "waiting", WAITING})
_STORED = {
    State.QUEUED: "queued",
    State.RUNNING: "running",
    State.BACKOFF: "running",
    State.OBSERVING: "waiting",
    State.NEEDS_OPERATOR: WAITING,
    State.SUCCEEDED: "succeeded",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
}
KEEP: Any = object()
_KEEP = KEEP
_CANCEL_EFFECT_UNKNOWN = (
    "run-switch.cancel-effect-unknown: the child operation was still running when "
    "the stop budget ended; its own lifecycle owns it"
)

#: Stops an idempotent child of ``job`` (inside the caller's session); ``True`` when
#: nothing is left running.
Stopper = Callable[[Session, Job, datetime], bool]


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _count(value: object) -> int:
    """``retry_attempt`` is the next attempt: the count so far is one less."""

    return max(value - 1, 0) if type(value) is int else 0


def _intent_ordinal(job: Job) -> int | None:
    ordinal = (
        job.payload.get("workload_intent_ordinal")
        if isinstance(job.payload, Mapping)
        else None
    )
    return ordinal if type(ordinal) is int else None


def set_member_state(entry: MutableMapping[str, object], state: str) -> None:
    """The only writer of a member's progress ``state`` (a projection, not a row)."""

    entry["state"] = state


class RunSwitchAdapter:
    """``KindAdapter`` for a stored Run/Switch operation."""

    kind = KIND

    def __init__(
        self,
        session: Session | None = None,
        *,
        sessions: sessionmaker[Session] | None = None,
        clock: Callable[[], datetime] | None = None,
        stopper: Stopper | None = None,
    ) -> None:
        self._session = session
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stopper = stopper
        # Unbound (no session, no factory): decisions and writes only; the reads
        # (``children``, ``observe``, ``stop``) need a binding.
        self._orders = (
            None
            if session is None and sessions is None
            else AgentOperationAdapter(session, sessions=sessions, clock=clock)
        )

    # ------------------------------------------------------------- plumbing

    @contextmanager
    def _read(self):
        if self._session is not None:
            yield self._session
        else:
            assert self._sessions is not None, "bind the adapter to read children"
            with self._sessions() as session:
                yield session

    def now(self) -> datetime:
        return aware(self._clock())

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: Job) -> Lifecycle:
        """Map a stored (possibly legacy) operation onto the core."""

        result = stored.result
        return self.lifecycle(
            stored, result if isinstance(result, Mapping) else {}, self.now()
        )

    def lifecycle(
        self, job: Job, progress: Mapping[str, object], now: datetime
    ) -> Lifecycle:
        """The lifecycle row of a stored operation, defaulting what a legacy row lacks.

        A ``running`` operation with no clock is ``running`` (a live child is never
        touched); one holding a clock and a retry reason is in ``backoff``, one
        holding only a clock is being observed.  A legacy ``waiting-for-operator``
        operation with a clock is observed (what its view already presents); one
        without is ``needs-operator`` and is re-evaluated by the first decision
        (rules 1 and 3: nothing advertises an action, so it is retried).
        """

        now = aware(now)
        due = _when(progress.get("observation_due_at"))
        retry_reason = progress.get("retry_reason")
        retrying = isinstance(retry_reason, str) and bool(retry_reason)
        stored = job.state
        if stored == "queued":
            state = State.QUEUED
        elif stored == "running":
            if due is None:
                state = State.RUNNING
            else:
                state = State.BACKOFF if retrying else State.OBSERVING
        elif stored == "waiting":
            state = State.BACKOFF if retrying and due is not None else State.OBSERVING
        elif stored == WAITING:
            state = State.OBSERVING if due is not None else State.NEEDS_OPERATOR
        elif stored == "succeeded":
            state = State.SUCCEEDED
        elif stored == "failed":
            state = State.FAILED
        elif stored == "cancelled":
            state = State.CANCELLED
        else:  # a state this adapter does not know: re-evaluate it
            state = State.NEEDS_OPERATOR
        child = progress.get("child_operation_id")
        issued = isinstance(child, str) and bool(child)
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        else:
            effect = Effect.ISSUED if issued else Effect.NONE
        requested_at, request_key = self._cancel_request(progress, now)
        count = _count(progress.get("retry_attempt"))
        return Lifecycle(
            id=job.id,
            kind=KIND,
            state=state,
            attempt=1 if issued else 0,
            next_action_at=None if state in TERMINAL_STATES else due,
            # One stored counter (``retry_attempt``) serves the retry backoff and the
            # observation count; only one is live at a time, and a cancel starts
            # its own count (the owner resets the counter when it records one).
            retry_count=count,
            observe_count=count,
            intent_ordinal=_intent_ordinal(job),
            cancel_requested_at=requested_at,
            cancel_request_key=request_key,
            effect=effect,
            reason=job.status_reason,
        )

    @staticmethod
    def _cancel_request(
        progress: Mapping[str, object], now: datetime
    ) -> tuple[datetime | None, str | None]:
        cancellation = progress.get("cancellation")
        if not isinstance(cancellation, Mapping):
            return None, None
        requested = _when(cancellation.get("requested_at")) or now
        key = cancellation.get("request_key")
        return requested, key if isinstance(key, str) else None

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """A composite has no effect of its own: its children own theirs."""

        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The phase executor is driven by the operation's own tick, not here."""

        return Dispatch(issued=False)

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        """The Spark orders of the operation's current child (a composite)."""

        with self._read() as session:
            job = session.get(Job, row.id)
            result = None if job is None else job.result
            child = (
                result.get("child_operation_id")
                if isinstance(result, Mapping)
                else None
            )
            if not isinstance(child, str) or not child:
                return ()
            orders = session.scalars(
                select(StoredOperation)
                .where(StoredOperation.parent_job_id == child)
                .order_by(StoredOperation.id)
            ).all()
            return tuple(
                AgentOperationAdapter(session, clock=self._clock).adopt(order)
                for order in orders
            )

    def observe(self, row: Lifecycle) -> Observed:
        """Read-only: what the children say about the effect.

        No child means nothing was issued; every order of the child succeeded means
        the phase's effect is established; anything else is unknown, and the owner
        observes again.
        """

        with self._read() as session:
            job = session.get(Job, row.id)
            result = None if job is None else job.result
            child = (
                result.get("child_operation_id")
                if isinstance(result, Mapping)
                else None
            )
        if not isinstance(child, str) or not child:
            return Observed(Effect.NONE)
        if aggregate(self.children(row)) is State.SUCCEEDED:
            return Observed(Effect.ESTABLISHED)
        return Observed(Effect.UNKNOWN, "the child operation has not ended")

    def stop(self, row: Lifecycle) -> StopResult:
        """Idempotent: stop what the child still runs, when it can be stopped."""

        with self._read() as session:
            job = session.get(Job, row.id)
            if job is None:
                return StopResult.CONFIRMED
            result = job.result
            child = (
                result.get("child_operation_id")
                if isinstance(result, Mapping)
                else None
            )
            if not isinstance(child, str) or not child:
                return StopResult.CONFIRMED
            if self._stopper is not None and self._stopper(session, job, self.now()):
                return StopResult.CONFIRMED
        return StopResult.UNCONFIRMED

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """None: a Run/Switch has no ``resume`` or ``retire``; its Stop is Cancel."""

        return ()

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        job: Job,
        progress: MutableMapping[str, object] | None,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
        *,
        visible: str | None = None,
        reason: str | None = _KEEP,
        failure_code: str | None = None,
        retryable: bool = False,
        retry_reason: str | None = _KEEP,
    ) -> None:
        """Project a core decision onto the stored operation; the only such writer.

        ``progress`` is the caller's progress document, changed in place (the caller
        persists it with its own validation).  ``visible`` is the stored label for a
        non-terminal state the vocabulary spells two ways (``running`` or ``waiting``).
        """

        now = aware(now)
        state = visible if visible and not after.terminal else _STORED[after.state]
        job.state = state
        if reason is not _KEEP:
            job.status_reason = None if reason is None else reason[:_MAX_REASON]
        if progress is not None:
            if after.state in {State.BACKOFF, State.OBSERVING}:
                if after.next_action_at is not None:
                    progress["observation_due_at"] = aware(
                        after.next_action_at
                    ).isoformat()
                count = (
                    after.retry_count
                    if after.state is State.BACKOFF
                    else after.observe_count
                )
                if count > 0:
                    progress["retry_attempt"] = count + 1
            if retry_reason is not _KEEP:
                if retry_reason is None:
                    progress.pop("retry_reason", None)
                else:
                    progress["retry_reason"] = retry_reason[:_MAX_REASON]
            if after.terminal:
                if (
                    after.state is State.CANCELLED
                    and after.effect is not Effect.UNKNOWN
                ):
                    # The child's phase ended: nothing of it is outstanding.  An
                    # unknown effect keeps the child's identity as the evidence.
                    progress["child_operation_id"] = None
                progress["retryable"] = retryable
                if after.state is State.FAILED:
                    progress["failed_phase"] = progress.get("phase")
                    if failure_code is None:
                        progress.pop("failure_code", None)
                    else:
                        progress["failure_code"] = failure_code
            elif after.state is State.BACKOFF:
                progress["retryable"] = False
        job.updated_at = now

    # ---------------------------------------------------------------- settle

    def settle(
        self,
        job: Job,
        progress: MutableMapping[str, object],
        event: Event,
        now: datetime,
        **write: Any,
    ) -> Lifecycle:
        """Feed ``event`` to the core and write what it decides, inline.

        The caller holds the operation's lock and persists ``progress`` afterwards.
        A cancel that cannot be confirmed is driven by the core's stop budget, so
        it ends ``cancelled`` with the child's effect unknown and never waits.
        """

        return self.settled(job, progress, event, now, **write).row

    def settled(
        self,
        job: Job,
        progress: MutableMapping[str, object],
        event: Event,
        now: datetime,
        *,
        unflag: bool = False,
        **write: Any,
    ) -> Settled:
        """:meth:`settle`, reporting whether anything changed or ran.

        A decision that left the row alone (a tick whose time has not come) writes
        nothing, so an owner reports "advanced" only when it did.
        """

        now = aware(now)
        row = self.lifecycle(job, progress, now)
        if unflag:
            # The owner recorded the request in the progress before asking; the
            # core sets its own monotonic flag from the event, so the request is
            # not mistaken for one that is already being driven.
            row = replace(row, cancel_requested_at=None, cancel_request_key=None)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            options = dict(write)
            if (
                after.state is State.OBSERVING
                and after.cancel_requested
                and options.get("visible") is None
            ):
                # A cancel being driven keeps showing what the operation was doing.
                options["visible"] = (
                    job.state
                    if job.state in {"queued", "running", "waiting"}
                    else "running"
                )
            if after.state is State.CANCELLED:
                options["reason"] = self._cancelled_reason(progress, after, options)
            self.apply(job, progress, before, after, now, **options)
            return True

        def residue(_row: Lifecycle, _reason: str) -> None:
            return None  # the residue note is the reason ``apply`` wrote

        return settle_event(row, event, self, now, save=save, residue=residue)

    @staticmethod
    def _cancelled_reason(
        progress: Mapping[str, object], after: Lifecycle, write: Mapping[str, Any]
    ) -> str | None:
        given = write.get("reason", _KEEP)
        explicit = given is not _KEEP and given is not None
        cancellation = progress.get("cancellation")
        base = (
            given
            if explicit
            else cancellation.get("reason")
            if isinstance(cancellation, Mapping)
            and isinstance(cancellation.get("reason"), str)
            else None
        )
        if after.effect is Effect.UNKNOWN and not explicit:
            return (
                f"{base}; {_CANCEL_EFFECT_UNKNOWN}" if base else _CANCEL_EFFECT_UNKNOWN
            )
        return base

    def decide(
        self, job: Job, progress: Mapping[str, object], event: Event, now: datetime
    ) -> Decision:
        """The core's decision for ``event`` without writing it (tests and probes)."""

        now = aware(now)
        return transition(self.lifecycle(job, progress, now), event, self, now)

    # ------------------------------------------- the operation's own events

    def retry(
        self,
        job: Job,
        progress: MutableMapping[str, object],
        reason: str,
        now: datetime,
        *,
        describe: Callable[[datetime], str] | None = None,
        visible: str | None = None,
        record_reason: bool = True,
        reset_on_change: bool = False,
    ) -> Lifecycle:
        """A phase could not be settled: observe it again later (rules 1, 2 and 5).

        The retry clock is the core's (stable jittered backoff); the operation
        never fails for it.  A cancel in flight wins and is driven instead.
        """

        if reset_on_change and progress.get("retry_reason") != reason[:_MAX_REASON]:
            progress.pop("retry_attempt", None)
        row = self.settle(
            job,
            progress,
            Reported(Outcome.FAILED, retryable=True, reason=reason),
            now,
            visible=visible,
            retry_reason=reason if record_reason else _KEEP,
            reason=_KEEP,
        )
        if row.state is State.BACKOFF and row.next_action_at is not None:
            due = aware(row.next_action_at)
            text = (
                describe(due)
                if describe is not None
                else f"{reason[:400]}; next attempt at {due.isoformat()}"
            )
            job.status_reason = text[:_MAX_REASON]
        return row

    def fail(
        self,
        job: Job,
        progress: MutableMapping[str, object],
        reason: str,
        now: datetime,
        *,
        failure_code: str | None = None,
        retryable: bool = False,
    ) -> Lifecycle:
        """A definite end: a refusal that retrying cannot change (rule 5)."""

        return self.settle(
            job,
            progress,
            Reported(Outcome.FAILED, retryable=False, reason=reason),
            now,
            reason=reason,
            failure_code=failure_code,
            retryable=retryable,
        )

    def succeed(
        self, job: Job, progress: MutableMapping[str, object], now: datetime
    ) -> Lifecycle:
        return self.settle(
            job, progress, Reported(Outcome.OK), now, reason=None, retry_reason=None
        )

    def request_cancel(
        self,
        job: Job,
        progress: MutableMapping[str, object],
        now: datetime,
        *,
        reason: str | None = None,
    ) -> Lifecycle:
        """Rule 4: a cancel completes.  Nothing issued ends at once; otherwise the
        child is stopped and observed, and the budget bounds the wait."""

        return self.settled(
            job,
            progress,
            CancelRequested(self._cancel_request(progress, self.now())[1], reason),
            now,
            unflag=True,
        ).row

    def tick_cancel(
        self, job: Job, progress: MutableMapping[str, object], now: datetime
    ) -> bool:
        """One stop attempt of a cancel in flight (the core spaces and bounds them).

        Returns whether the cancel moved: a tick that is not due yet changes nothing.
        """

        settled = self.settled(job, progress, Tick(), now)
        return bool(settled.changed or settled.commands)

    def cancelled(
        self,
        job: Job,
        progress: MutableMapping[str, object],
        now: datetime,
        *,
        reason: str | None,
        effect: Effect = Effect.STOPPED,
    ) -> Lifecycle:
        """A definite cancel: superseded by a newer intent, or translated to a Stop."""

        return self.settle(
            job,
            progress,
            Reported(Outcome.CANCELLED, effect=effect, reason=reason),
            now,
            reason=reason,
        )

    def reject(self, job: Job, reason: str, now: datetime) -> Lifecycle:
        """Retain an operation whose stored contract cannot be read (never repaired).

        Its plan or progress is the evidence of what was issued, so nothing is
        replaced; the row ends ``failed`` with the reason and stops being advanced.
        """

        return self.settle(
            job,
            {},
            Reported(Outcome.FAILED, retryable=False, reason=reason),
            now,
            reason=reason,
        )

    # --------------------------------------------------------- projections

    def project(
        self,
        job: Job,
        progress: MutableMapping[str, object] | None,
        now: datetime,
        *,
        state: State,
        reason: str | None = _KEEP,
        due: datetime | None = None,
        visible: str | None = None,
    ) -> Lifecycle:
        """Show what the child or the phase is doing (a projection, not a decision):
        ``running``, ``queued``, or observing until ``due``."""

        now = aware(now)
        before = self.lifecycle(job, progress or {}, now)
        if before.terminal:
            return before
        after = replace(
            before,
            state=state,
            next_action_at=due if state is State.OBSERVING else None,
        )
        self.apply(job, progress, before, after, now, visible=visible, reason=reason)
        return after

    def heal(
        self, job: Job, progress: MutableMapping[str, object], now: datetime
    ) -> Lifecycle:
        """Re-evaluate a legacy row that waits with no clock (``waiting-for-operator``).

        Rules 1 and 3: no action is advertised and nothing is irreversible here, so
        the operation is retried, never left waiting for a person.
        """

        row = self.lifecycle(job, progress, now)
        if row.state is not State.NEEDS_OPERATOR:
            return row
        return self.settle(job, progress, Tick(), now, reason=_KEEP, retry_reason=_KEEP)

    @staticmethod
    def new_operation(*, allowed: bool, **fields: Any) -> Job:
        """A new operation: ``queued`` when its plan is allowed, else ``waiting``."""

        return Job(state="queued" if allowed else "waiting", **fields)


__all__ = [
    "KEEP",
    "KIND",
    "LIVE_STATES",
    "RunSwitchAdapter",
    "set_member_state",
]
