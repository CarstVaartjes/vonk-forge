"""The lifecycle adapter of an agent-upgrade rollout (``Job`` of kind ``agent-upgrade``).

A rollout is a **composite** over its ``agent.upgrade.v1`` orders (one Spark at a
time).  It has no effect of its own, so it is never irreversible, never executes
anything, and never waits for an operator: the orders carry the effect, the fenced
retry and the cancel (they are decided by
:class:`~vonk_control.lifecycle.agent_operation.AgentOperationAdapter`), and the
rollout *follows* them.  ``agent_upgrades`` keeps the plan, the eligibility rules
and the dispatch of the next Spark; this module is the **only** writer of the
rollout's ``Job.state`` (and of the worker-dispatch ``JobAttempt`` a legacy rollout
may still hold).

========================  =====================================================
core state                stored rollout
========================  =====================================================
``queued``                ``queued`` (an order is queued, running or retrying)
``running``               ``running``: legacy worker dispatch only, healed
``backoff``/``observing`` ``queued``
``needs-operator``        ``waiting-for-operator``: legacy only, healed and never
                          written (no rollout action exists)
``succeeded``/``failed``  ``succeeded``/``failed``
``cancelled``             ``cancelled`` (superseded by a newer request)
========================  =====================================================
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from .. import job_states
from ..agent_operation_facts import aware
from ..models import Job, JobAttempt
from .adapter import Dispatch
from .core import transition
from .reconciler import Settled
from .reconciler import settle as settle_event
from .types import (
    TERMINAL_STATES,
    Decision,
    Effect,
    Event,
    Lifecycle,
    Observed,
    Outcome,
    Reported,
    State,
    StopResult,
)

KIND = "agent-upgrade"
_MAX_REASON = 512
#: Stored states a rollout can still advance from.
LIVE_STATES = job_states.words(State.QUEUED, State.RUNNING, State.NEEDS_OPERATOR)
_STORED = {
    State.QUEUED: "queued",
    State.RUNNING: "running",
    State.BACKOFF: "queued",
    State.OBSERVING: "queued",
    State.NEEDS_OPERATOR: "queued",  # never written as a wait: it is healed
    State.SUCCEEDED: "succeeded",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
}
KEEP: Any = object()
#: The reason the generic worker once gave a rollout it could not dispatch.
UNSUPPORTED_DISPATCH = "unsupported job kind: agent-upgrade"


class AgentUpgradeAdapter:
    """``KindAdapter`` for a stored agent-upgrade rollout."""

    kind = KIND

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: Job) -> Lifecycle:
        """Map a stored (possibly legacy) rollout onto the core.

        A legacy ``running`` rollout (a worker dispatch) is observed, a legacy
        ``waiting-for-operator`` one is ``needs-operator`` and is re-evaluated by
        the first decision (nothing advertises an action, so it is projected from
        its orders).  A superseding request is the monotonic cancel request.
        """

        stored_state = stored.state
        if stored_state == "queued":
            state = State.QUEUED
        elif stored_state == "running":
            state = State.OBSERVING
        elif stored_state == "succeeded":
            state = State.SUCCEEDED
        elif stored_state == "failed":
            state = State.FAILED
        elif stored_state == "cancelled":
            state = State.CANCELLED
        else:  # waiting-for-operator, or a state this adapter does not know
            state = State.NEEDS_OPERATOR
        result = stored.result if isinstance(stored.result, Mapping) else {}
        superseded = result.get("superseded_by")
        issued = stored.current_attempt > 0
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        else:
            effect = Effect.ISSUED if issued else Effect.NONE
        return Lifecycle(
            id=stored.id,
            kind=KIND,
            state=state,
            attempt=stored.current_attempt,
            cancel_requested_at=(
                aware(stored.updated_at) if isinstance(superseded, str) else None
            ),
            cancel_request_key=superseded if isinstance(superseded, str) else None,
            effect=effect,
            reason=stored.status_reason,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """A composite has no effect of its own: its orders own theirs."""

        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        """The dpkg fence belongs to the orders (``AGENT_UPGRADE_RECOVERY_FENCE``)."""

        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        return Dispatch(issued=False)  # the rollout dispatches through its owner

    def observe(self, row: Lifecycle) -> Observed:
        return Observed(Effect.UNKNOWN, "the rollout follows its orders")

    def stop(self, row: Lifecycle) -> StopResult:
        return StopResult.CONFIRMED  # the orders are stopped by their own adapter

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """None: a rollout has no ``resume`` of its own; its orders retry by themselves."""

        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()  # read through the owner, which holds the session

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        job: Job,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
        *,
        reason: str | None = KEEP,
    ) -> None:
        """Project a core decision onto the stored rollout; the only such writer."""

        job.state = _STORED[after.state]
        if reason is not KEEP:
            job.status_reason = None if reason is None else reason[:_MAX_REASON]
        job.updated_at = aware(now)

    def settled(
        self,
        job: Job,
        event: Event,
        now: datetime,
        *,
        reason: str | None = KEEP,
    ) -> Settled:
        now = aware(now)
        row = self.adopt(job)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            self.apply(job, before, after, now, reason=reason)
            return True

        return settle_event(
            row, event, self, now, save=save, residue=lambda _row, _why: None
        )

    def decide(self, job: Job, event: Event, now: datetime) -> Decision:
        """The core's decision for ``event`` without writing it (tests and probes)."""

        now = aware(now)
        return transition(self.adopt(job), event, self, now)

    # ------------------------------------------------- the rollout's events

    def project(
        self, job: Job, now: datetime, *, reason: str | None = KEEP
    ) -> Lifecycle:
        """Show that an order is queued, running or retrying (a projection of the
        orders, not a decision).  A legacy wait or dispatch is healed to ``queued``."""

        before = self.adopt(job)
        if before.terminal:
            return before
        after = replace(before, state=State.QUEUED)
        self.apply(job, before, after, now, reason=reason)
        return after

    def succeed(
        self, job: Job, now: datetime, *, reason: str | None = None
    ) -> Lifecycle:
        return self.settled(job, Reported(Outcome.DONE), now, reason=reason).row

    def fail(self, job: Job, now: datetime, reason: str) -> Lifecycle:
        """A definite end: a failed order or a stored plan that cannot be read."""

        return self.settled(
            job,
            Reported(Outcome.FAILED, retryable=False, reason=reason),
            now,
            reason=reason,
        ).row

    def cancelled(self, job: Job, now: datetime, reason: str) -> Lifecycle:
        """Superseded by a newer request, once its orders have settled."""

        return self.settled(
            job,
            Reported(Outcome.CANCELLED, effect=Effect.STOPPED, reason=reason),
            now,
            reason=reason,
        ).row

    def reopen(
        self, job: Job, now: datetime, *, reason: str | None = None
    ) -> Lifecycle:
        """An ended rollout whose orders are still owed work runs again (the
        operator's ``resume`` of a rollout the generic worker once failed)."""

        before = self.adopt(job)
        after = replace(before, state=State.QUEUED, effect=Effect.ISSUED)
        self.apply(job, before, after, now, reason=reason)
        return after

    def expire_worker_attempt(self, attempt: JobAttempt) -> None:
        """A legacy worker dispatch whose lease lapsed: record it as expired."""

        job_states.lapse(attempt)

    @staticmethod
    def new_rollout(*, allowed: bool, **fields: Any) -> Job:
        """A new rollout: ``queued``, or ``succeeded`` when no Spark needs it."""

        return Job(state="queued" if allowed else "succeeded", **fields)


__all__ = [
    "KEEP",
    "KIND",
    "LIVE_STATES",
    "TERMINAL_STATES",
    "UNSUPPORTED_DISPATCH",
    "AgentUpgradeAdapter",
]
