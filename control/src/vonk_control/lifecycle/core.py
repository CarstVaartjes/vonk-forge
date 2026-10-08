"""The one transition function of the shared lifecycle core.

``transition(row, event, adapter, now)`` is pure and total: every state and event
returns a :class:`Decision`, and it never raises for bookkeeping.  It encodes the
seven rules of the blocker audit (section 5.2), each of which is a test in
``control/tests/test_lifecycle_core.py``:

1. **Never executed, or idempotent, is retried.**  ``attempt == 0``, a known
   ``none`` effect, or a kind that is not irreversible goes to ``backoff`` with a
   stable, bounded delay.  It never waits for an operator.
2. **Uncertain effect is observed first.**  A lapsed lease, an uncertain report
   or a retryable failure of irreversible, possibly executed work is observed;
   an established effect succeeds, none retries, unknown is observed again at a
   bounded rate.
3. **No operator wait without an advertised action.**  ``needs-operator`` is only
   built by :func:`_park`, which requires an irreversible kind, an effect that is
   still unknown after the observation budget, and a non-empty
   ``adapter.actions(row)``.  Otherwise the row keeps observing.
4. **A cancel always completes.**  The request is monotonic.  Work that never ran
   is cancelled at once; other work is stopped (idempotently) and observed until
   stopped, and after ``STOP_BUDGET`` unconfirmed stops it is cancelled anyway
   with a residue record.  A cancelled row never waits for an operator.
5. **Bookkeeping uncertainty is unknown, then reconcile.**  Adapters raise only
   at submit time; anything else reaches this function as an uncertain report or
   an ``Observed(unknown)`` and follows rule 2.
6. **Supersede, not block.**  A newer intent cancels the older row with a
   ``CancelRequested`` event, which follows rule 4.
7. **Fail open, fence closed.**  A report or heartbeat under a fence that is not
   the row's is stale and dropped without effect; everything else is accepted.

The function does not read a clock, a database or an executor: time is the
``now`` argument, the kind's facts are ``adapter.irreversible`` and
``adapter.actions`` (read-only), and the work itself is returned as commands for
the reconciler to run.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ..recovery_policy import RecoveryPolicy
from .adapter import KindAdapter
from .types import (
    ActionName,
    CancelRequested,
    Claimed,
    Command,
    Decision,
    Effect,
    Event,
    Execute,
    Heartbeat,
    LeaseLapsed,
    Lifecycle,
    Observe,
    Observed,
    OperatorAction,
    Outcome,
    RecordResidue,
    Reported,
    State,
    Stop,
    Submitted,
    Tick,
)

#: How many observations of an irreversible, unknown effect precede an operator
#: wait (rule 3).  Idempotent kinds never wait: they are retried (rule 1).
OBSERVE_BUDGET = 8
#: How many stops of a cancelled row are issued before it is cancelled with a
#: residue record (rule 4).  Together with the stop backoff this bounds a cancel
#: to ``STOP_BUDGET`` ticks.
STOP_BUDGET = 6
#: Stable jittered backoff, shared with the rest of the Controller.  The rate is
#: bounded, not the lifetime: work with a current intent is always retried.
RECOVERY = RecoveryPolicy()
#: Operator actions that restart work, and the one that abandons the effect.
_RESUME_ACTIONS = frozenset({ActionName.RESUME, ActionName.RETRY})


def transition(
    row: Lifecycle, event: Event, adapter: KindAdapter, now: datetime
) -> Decision:
    """Decide the next row and the commands for ``event``; total and pure."""

    if row.terminal:
        return Decision(row)
    if _stale(row, event):
        return Decision(row)
    match event:
        case CancelRequested():
            return _cancel(row, event, now)
        case Submitted():
            return _submitted(row)
        case Claimed():
            return _claimed(row, event, now)
        case Heartbeat():
            return _heartbeat(row, event)
        case Reported():
            return _reported(row, event, adapter, now)
        case LeaseLapsed():
            return _lapsed(row, adapter, now, event.reason)
        case Observed():
            return _observed(row, event, adapter, now)
        case OperatorAction():
            return _operator(row, event, adapter, now)
        case Tick():
            return _tick(row, adapter, now)
    return Decision(row)  # an event type added without a rule is ignored


# --------------------------------------------------------------- predicates


def _stale(row: Lifecycle, event: Event) -> bool:
    """Rule 7: a report or heartbeat under another fence is dropped."""

    fence = getattr(event, "fence", None)
    return (
        isinstance(event, (Reported, Heartbeat))
        and fence is not None
        and row.fence is not None
        and fence != row.fence
    )


def never_executed(row: Lifecycle) -> bool:
    """No attempt started, or the effect is known to be absent."""

    return row.attempt == 0 or row.effect is Effect.NONE


def blind_retry_is_safe(
    row: Lifecycle, adapter: KindAdapter, effect: Effect | None = None
) -> bool:
    """Rule 1: repeating ``execute`` cannot repeat a visible effect."""

    known = row.effect if effect is None else effect
    return row.attempt == 0 or known is Effect.NONE or not adapter.irreversible(row)


# ---------------------------------------------------------------- builders


def _due(
    row: Lifecycle,
    count: int,
    now: datetime,
    adapter: KindAdapter | None = None,
    retry_after: datetime | None = None,
) -> datetime:
    """Stable bounded backoff, never earlier than the kind's own fence."""

    not_before = None if adapter is None else adapter.retry_not_before(row, now)
    if retry_after is not None:
        not_before = retry_after if not_before is None else max(not_before, retry_after)
    scheduled = RECOVERY.next_attempt(
        row.id, max(count, 1), now, retry_after=not_before, ongoing_intent=True
    )
    assert scheduled is not None  # ongoing_intent never exhausts
    return min(scheduled, row.recovery_deadline) if row.recovery_deadline else scheduled


def _retry(
    row: Lifecycle,
    now: datetime,
    adapter: KindAdapter,
    *,
    effect: Effect,
    reason: str | None,
    retry_after: datetime | None = None,
) -> Decision:
    """Rule 1: schedule the next attempt; never an operator wait."""

    count = row.retry_count + 1
    return Decision(
        replace(
            row,
            state=State.BACKOFF,
            retry_count=count,
            observe_count=0,
            next_action_at=_due(row, count, now, adapter, retry_after),
            lease_deadline=None,
            effect=effect,
            reason=reason or row.reason,
        )
    )


def _observe(row: Lifecycle, now: datetime, reason: str | None) -> Decision:
    """Rule 2: inspect the effect before anything else."""

    count = row.observe_count + 1
    return Decision(
        replace(
            row,
            state=State.OBSERVING,
            observe_count=count,
            next_action_at=_due(row, count, now),
            lease_deadline=None,
            effect=Effect.UNKNOWN if row.effect is Effect.NONE else row.effect,
            reason=reason or row.reason,
        ),
        (Observe(),),
    )


def _park(
    row: Lifecycle, adapter: KindAdapter, now: datetime, reason: str | None
) -> Decision:
    """The only constructor of ``needs-operator`` (rule 3).

    An operator wait needs an irreversible kind, an effect that is still unknown
    after the observation budget, and an advertised action.  Without one the
    row keeps observing at a bounded rate.
    """

    waiting = replace(
        row,
        state=State.NEEDS_OPERATOR,
        next_action_at=None,
        lease_deadline=None,
        effect=Effect.UNKNOWN,
        reason=reason or row.reason,
    )
    if (
        row.recovery_deadline is None
        and adapter.irreversible(row)
        and not blind_retry_is_safe(row, adapter)
        and row.observe_count >= OBSERVE_BUDGET
        and adapter.actions(waiting)
    ):
        return Decision(waiting)
    count = max(row.observe_count, 1)
    return Decision(
        replace(
            row,
            state=State.OBSERVING,
            observe_count=count,
            next_action_at=_due(row, count, now),
            lease_deadline=None,
            effect=Effect.UNKNOWN,
            reason=reason or row.reason,
        ),
        (Observe(),),
    )


def _uncertain(
    row: Lifecycle,
    adapter: KindAdapter,
    now: datetime,
    *,
    effect: Effect | None = None,
    reason: str | None = None,
    retry_after: datetime | None = None,
) -> Decision:
    """Rules 1 and 2 for an effect that may or may not have happened."""

    if row.recovery_deadline is not None and now >= row.recovery_deadline:
        return _observe(row, now, reason)
    if blind_retry_is_safe(row, adapter, effect):
        return _retry(
            row,
            now,
            adapter,
            effect=effect or row.effect,
            reason=reason,
            retry_after=retry_after,
        )
    return _observe(row, now, reason)


def _unknown_after_observation(
    row: Lifecycle, adapter: KindAdapter, now: datetime, reason: str | None
) -> Decision:
    if not adapter.irreversible(row) or blind_retry_is_safe(row, adapter):
        return _retry(row, now, adapter, effect=row.effect, reason=reason)
    if row.observe_count >= OBSERVE_BUDGET:
        return _park(row, adapter, now, reason)
    return Decision(
        replace(
            row,
            state=State.OBSERVING,
            next_action_at=_due(row, row.observe_count, now),
            reason=reason or row.reason,
        )
    )


# ------------------------------------------------------------------- cancel


def _with_cancel_flag(row: Lifecycle, now: datetime, key: str | None) -> Lifecycle:
    """Monotonic: set once, never cleared, the first request key is kept."""

    if row.cancel_requested:
        return row
    return replace(row, cancel_requested_at=now, cancel_request_key=key)


def _cancel(row: Lifecycle, event: CancelRequested, now: datetime) -> Decision:
    if row.cancel_requested and row.state is State.OBSERVING:
        return Decision(row)  # the cancel is already being driven to its end
    flagged = _with_cancel_flag(row, now, event.request_key)
    return _advance_cancel(flagged, now, observed=None, reason=event.reason)


def _advance_cancel(
    row: Lifecycle,
    now: datetime,
    *,
    observed: Effect | None,
    reason: str | None = None,
    issue_stop: bool = True,
) -> Decision:
    """Rule 4.  ``observed`` is what a stop or an inspection just proved.

    ``issue_stop=False`` records an answer without issuing the next stop: the
    next ``Tick`` does, at the bounded rate, so a chatty executor cannot spend
    the stop budget in one pass.
    """

    if observed in (Effect.STOPPED, Effect.NONE) or (
        observed is None and never_executed(row)
    ):
        return Decision(
            replace(
                row,
                state=State.CANCELLED,
                effect=observed or Effect.NONE,
                next_action_at=None,
                lease_deadline=None,
                reason=reason or row.reason or "cancelled",
            )
        )
    if row.observe_count >= STOP_BUDGET:
        why = "stop stayed unconfirmed; cancelled with the effect unknown"
        return Decision(
            replace(
                row,
                state=State.CANCELLED,
                effect=Effect.UNKNOWN,
                next_action_at=None,
                lease_deadline=None,
                reason=why,
            ),
            (RecordResidue(why),),
        )
    count = row.observe_count + 1 if issue_stop else row.observe_count
    return Decision(
        replace(
            row,
            state=State.OBSERVING,
            observe_count=count,
            next_action_at=_due(row, count, now),
            lease_deadline=None,
            effect=Effect.UNKNOWN if observed is None else observed,
            reason=reason or row.reason,
        ),
        (Stop(),) if issue_stop else (),
    )


# ------------------------------------------------------------------- events


def _submitted(row: Lifecycle) -> Decision:
    if row.state is State.QUEUED and row.attempt == 0:
        return Decision(replace(row, effect=Effect.NONE))
    return Decision(row)


def _claimed(row: Lifecycle, event: Claimed, now: datetime) -> Decision:
    claimable = row.state is State.QUEUED or (
        row.state is State.BACKOFF
        and (row.next_action_at is None or row.next_action_at <= now)
    )
    if not claimable or row.cancel_requested or event.attempt <= row.attempt:
        return Decision(row)
    return Decision(
        replace(
            row,
            state=State.RUNNING,
            attempt=event.attempt,
            fence=event.fence,
            lease_deadline=event.lease_deadline,
            next_action_at=event.lease_deadline,
            effect=Effect.ISSUED,
            observe_count=0,
        )
    )


def _heartbeat(row: Lifecycle, event: Heartbeat) -> Decision:
    if row.state is not State.RUNNING or (
        row.lease_deadline is not None and event.lease_deadline <= row.lease_deadline
    ):
        return Decision(row)
    return Decision(
        replace(
            row,
            lease_deadline=event.lease_deadline,
            next_action_at=event.lease_deadline,
        )
    )


def _reported(
    row: Lifecycle, event: Reported, adapter: KindAdapter, now: datetime
) -> Decision:
    # A definite report ends the row, cancelled or not: the executor says what
    # happened, and an owner that is waiting for exactly that outcome (a build
    # whose completion raced its cancel) must see it.
    if event.outcome is Outcome.DONE:
        return _end(row, State.SUCCEEDED, event.effect or Effect.ESTABLISHED, event)
    if event.outcome is Outcome.CANCELLED:
        return _end(row, State.CANCELLED, event.effect or Effect.STOPPED, event)
    if event.outcome is Outcome.FAILED and not event.retryable:
        # A refusal or an invalid request (rule 5): terminal by design.
        return _end(row, State.FAILED, event.effect or row.effect, event)
    if row.cancel_requested:
        # An uncertain report cannot undo the cancel: stop what it left behind.
        known = event.effect if event.effect in (Effect.STOPPED, Effect.NONE) else None
        return _advance_cancel(
            row,
            now,
            observed=known,
            reason=event.reason,
            # a cancel already being driven is not restarted by a late report
            issue_stop=row.state is not State.OBSERVING,
        )
    return _uncertain(
        row,
        adapter,
        now,
        effect=event.effect,
        reason=event.reason,
        retry_after=event.retry_after,
    )


def _end(row: Lifecycle, state: State, effect: Effect, event: Reported) -> Decision:
    return Decision(
        replace(
            row,
            state=state,
            effect=effect,
            next_action_at=None,
            lease_deadline=None,
            reason=event.reason or row.reason,
        )
    )


def _lapsed(
    row: Lifecycle,
    adapter: KindAdapter,
    now: datetime,
    reason: str | None = None,
) -> Decision:
    if row.state is not State.RUNNING:
        return Decision(row)
    if row.cancel_requested:
        return _advance_cancel(row, now, observed=None, reason=reason)
    return _uncertain(
        row, adapter, now, reason=reason or "the attempt can no longer report"
    )


def _observed(
    row: Lifecycle, event: Observed, adapter: KindAdapter, now: datetime
) -> Decision:
    if row.cancel_requested:
        return _advance_cancel(
            row, now, observed=event.effect, reason=event.reason, issue_stop=False
        )
    if row.state not in {State.OBSERVING, State.NEEDS_OPERATOR}:
        return Decision(row)
    if (
        row.recovery_deadline is not None
        and now >= row.recovery_deadline
        and event.effect is not Effect.ESTABLISHED
    ):
        why = event.reason or row.reason or "request recovery observation exhausted"
        return Decision(
            replace(
                row,
                state=State.FAILED,
                effect=event.effect,
                next_action_at=None,
                lease_deadline=None,
                reason=why,
            ),
            (RecordResidue(why),),
        )
    match event.effect:
        case Effect.ESTABLISHED:
            return Decision(
                replace(
                    row,
                    state=State.SUCCEEDED,
                    effect=Effect.ESTABLISHED,
                    next_action_at=None,
                    reason=event.reason or row.reason,
                )
            )
        case Effect.NONE | Effect.STOPPED:
            return _retry(row, now, adapter, effect=Effect.NONE, reason=event.reason)
        case _:
            return _unknown_after_observation(row, adapter, now, event.reason)


def _operator(
    row: Lifecycle, event: OperatorAction, adapter: KindAdapter, now: datetime
) -> Decision:
    if event.name not in adapter.actions(row):
        return Decision(row)  # an action the row does not advertise is refused
    if event.name == ActionName.STOP:
        if row.cancel_requested:
            return Decision(row)
        return _advance_cancel(
            _with_cancel_flag(row, now, None),
            now,
            observed=None,
            reason=event.reason or "stopped",
        )
    if row.state not in {State.NEEDS_OPERATOR, State.OBSERVING}:
        return Decision(row)  # only a wait (or an observation of one) is acted on
    if event.name in _RESUME_ACTIONS:
        return Decision(
            replace(
                row,
                state=State.QUEUED,
                retry_count=0,
                observe_count=0,
                next_action_at=None,
                reason=event.reason or "resumed by an operator",
            )
        )
    if event.name == ActionName.RETIRE:
        # The terminal counterpart of ``resume``: the order is failed, and its
        # effect stays unknown for the owner's exact cleanup to resolve.
        return Decision(
            replace(
                row,
                state=State.FAILED,
                effect=Effect.UNKNOWN,
                next_action_at=None,
                reason=event.reason or "retired by an operator",
            )
        )
    return Decision(row)


# --------------------------------------------------------------------- tick


def _tick(row: Lifecycle, adapter: KindAdapter, now: datetime) -> Decision:
    if row.cancel_requested:
        if row.next_action_at is not None and row.next_action_at > now:
            return Decision(row)
        return _advance_cancel(row, now, observed=None)
    if row.next_action_at is not None and row.next_action_at > now:
        return Decision(row)
    if (
        row.recovery_deadline is not None
        and now >= row.recovery_deadline
        and row.state is not State.RUNNING
    ):
        return _observe(row, now, row.reason)
    commands: tuple[Command, ...]
    match row.state:
        case State.QUEUED | State.BACKOFF:
            return Decision(
                replace(row, state=State.QUEUED, next_action_at=None), (Execute(),)
            )
        case State.RUNNING:
            if row.lease_deadline is not None and row.lease_deadline > now:
                return Decision(replace(row, next_action_at=row.lease_deadline))
            return _lapsed(row, adapter, now)
        case State.OBSERVING:
            if row.observe_count >= OBSERVE_BUDGET:
                return _unknown_after_observation(row, adapter, now, None)
            commands = (Observe(),)
            count = row.observe_count + 1
            return Decision(
                replace(row, observe_count=count, next_action_at=_due(row, count, now)),
                commands,
            )
        case State.NEEDS_OPERATOR:
            return _reevaluate_parked(row, adapter, now)
    return Decision(row)


def _reevaluate_parked(row: Lifecycle, adapter: KindAdapter, now: datetime) -> Decision:
    """Rules 1 to 3 for a parked row, e.g. a legacy ``waiting-for-operator``."""

    if row.effect is Effect.ESTABLISHED:
        return _observed(
            replace(row, state=State.OBSERVING),
            Observed(Effect.ESTABLISHED),
            adapter,
            now,
        )
    if row.effect is Effect.STOPPED:
        return _retry(row, now, adapter, effect=Effect.NONE, reason=row.reason)
    if blind_retry_is_safe(row, adapter):
        return _retry(row, now, adapter, effect=row.effect, reason=row.reason)
    if not adapter.actions(row):
        return _observe(replace(row, observe_count=0), now, row.reason)
    return Decision(replace(row, next_action_at=None))
