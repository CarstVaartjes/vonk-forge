"""The lifecycle adapter of a recipe operation (``Job`` of kind ``recipe.*``).

A recipe operation (install, uninstall, reconcile, start, stop, build, build
cleanup, JobRun activation and stop) is a **composite**: the work is done by its
Spark orders (:class:`~vonk_control.lifecycle.agent_operation.AgentOperationAdapter`
decides every retry, observation and cancel of those), or by the Controller itself
(a prebuilt-image pull).  ``recipe_operations`` keeps the plans, the phases, the
evidence and the admission; this module is the **only** writer of the parent job's
``state`` for the transitions that service owns:

* :meth:`RecipeOperationAdapter.new_job` creates the parent (``queued``,
  ``running``, or ``succeeded`` for a replayed or absent-effect receipt);
* :meth:`RecipeOperationAdapter.project` shows what the orders are doing
  (``running``) and :meth:`finish` records their definite end;
* :meth:`RecipeOperationAdapter.request_cancel` is rule 4: a parent whose orders
  never ran is cancelled at once, otherwise the cancel is recorded and the orders
  complete it (they are stopped, observed and bounded by the core's stop budget);
* :meth:`RecipeOperationAdapter.cancel_race` is the build whose completion raced
  its cancel: the cancel stays in flight (``running``) while the cleanup sweeper
  finishes it, instead of waiting for an operator (audit C7);
* :meth:`RecipeOperationAdapter.lifecycle` (the ``adopt`` hook) maps a stored,
  possibly legacy, parent onto the core.

Stored vocabulary is unchanged.  A parent never writes ``waiting-for-operator``
here: the only operator waits of a recipe operation are the orders' own (a build
or a JobRun whose effect is unknown, with ``resume``/``retire`` or Stop), and the
parent mirrors them through ``agent_jobs``' aggregate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..agent_operation_facts import aware
from ..models import Job
from .adapter import Dispatch
from .core import transition
from .types import (
    CancelRequested,
    Decision,
    Effect,
    Lifecycle,
    Observed,
    Outcome,
    Reported,
    State,
    StopResult,
)

KIND = "recipe-operation"
WAITING = "waiting-for-operator"
_MAX_REASON = 1024
_BORN = frozenset({"queued", "running", "succeeded"})
_STORED = {
    State.QUEUED: "queued",
    State.RUNNING: "running",
    State.BACKOFF: "running",
    State.OBSERVING: "running",
    State.NEEDS_OPERATOR: "running",  # never written: no action exists here
    State.SUCCEEDED: "succeeded",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
}


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def cancel_flagged(job: Job) -> bool:
    result = job.result
    return isinstance(result, Mapping) and result.get("cancel_requested") is True


class RecipeOperationAdapter:
    """``KindAdapter`` for the parent job of a recipe operation (a composite)."""

    kind = KIND

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: Job) -> Lifecycle:
        return self.lifecycle(stored, issued=True, now=aware(stored.updated_at))

    def lifecycle(
        self, job: Job, *, issued: bool, now: datetime | None = None
    ) -> Lifecycle:
        """The lifecycle row of a stored parent, defaulting what a legacy row lacks.

        ``issued`` is whether any order of the parent has started (a never-run
        parent cancels at once).  A legacy ``waiting-for-operator`` parent is
        ``needs-operator`` and is re-evaluated by its first decision.
        """

        stored = job.state
        state = {
            "queued": State.QUEUED,
            "running": State.RUNNING,
            "waiting": State.OBSERVING,
            "succeeded": State.SUCCEEDED,
            "failed": State.FAILED,
            "cancelled": State.CANCELLED,
        }.get(stored, State.NEEDS_OPERATOR)
        result = job.result if isinstance(job.result, Mapping) else {}
        requested = None
        key = None
        if result.get("cancel_requested") is True:
            requested = _when(result.get("cancel_requested_at")) or aware(
                now or job.updated_at
            )
            raw = result.get("cancel_request_id")
            key = raw if isinstance(raw, str) else None
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        else:
            effect = Effect.ISSUED if issued else Effect.NONE
        return Lifecycle(
            id=job.id,
            kind=KIND,
            state=state,
            attempt=1 if issued else 0,
            cancel_requested_at=requested,
            cancel_request_key=key,
            effect=effect,
            reason=job.status_reason,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """A composite has no effect of its own: its orders own theirs."""

        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        return Dispatch(issued=False)  # the Spark claims its own orders

    def observe(self, row: Lifecycle) -> Observed:
        return Observed(Effect.UNKNOWN, "the orders own the effect")

    def stop(self, row: Lifecycle) -> StopResult:
        return StopResult.UNCONFIRMED  # the orders are stopped by their own core

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()

    # ---------------------------------------------------------------- writes

    @staticmethod
    def new_job(**fields: Any) -> Job:
        """The only constructor of a recipe parent: it starts live or already done."""

        state = fields.get("state", "queued")
        if state not in _BORN:
            raise ValueError(f"a recipe operation cannot be created {state}")
        return Job(**fields)

    @staticmethod
    def _write(
        job: Job, after: Lifecycle, now: datetime, reason: str | None, keep: bool
    ) -> None:
        job.state = _STORED[after.state]
        if not keep:
            job.status_reason = None if reason is None else reason[:_MAX_REASON]
        job.updated_at = aware(now)

    def project(
        self, job: Job, now: datetime, *, reason: str | None = None, keep: bool = True
    ) -> None:
        """Show that the orders are working (``running``); a parent that ended stays."""

        before = self.lifecycle(job, issued=True, now=now)
        if before.terminal:
            job.updated_at = aware(now)
            return
        self._write(job, replace(before, state=State.RUNNING), now, reason, keep)

    def finish(
        self,
        job: Job,
        now: datetime,
        *,
        failed: bool,
        reason: str | None = None,
        keep: bool = True,
    ) -> Decision:
        """The orders ended: a definite success or failure (rule 5: not retried here,
        each order was retried by its own core before it reported)."""

        before = self.lifecycle(job, issued=True, now=now)
        outcome = Outcome.FAILED if failed else Outcome.OK
        decision = transition(
            before, Reported(outcome, retryable=False, reason=reason), self, aware(now)
        )
        self._write(job, decision.row, now, reason, keep)
        return decision

    def cancelled(
        self,
        job: Job,
        now: datetime,
        *,
        reason: str | None = None,
        keep: bool = True,
    ) -> Decision:
        """A definite cancel: nothing ran, an order's cleanup was confirmed, or a
        newer intent superseded it."""

        before = self.lifecycle(job, issued=True, now=now)
        decision = transition(
            before,
            Reported(Outcome.CANCELLED, effect=Effect.STOPPED, reason=reason),
            self,
            aware(now),
        )
        self._write(job, decision.row, now, reason, keep)
        return decision

    def request_cancel(
        self,
        job: Job,
        now: datetime,
        *,
        issued: bool,
        request_key: str | None,
        reason: str | None,
    ) -> Lifecycle:
        """Rule 4: a parent whose orders never ran is cancelled now; otherwise the
        request is recorded and the orders (stopped and bounded by their core)
        complete it.  Never an operator wait."""

        before = self.lifecycle(job, issued=issued, now=now)
        if before.terminal:
            return before
        before = replace(before, cancel_requested_at=None, cancel_request_key=None)
        decision = transition(
            before, CancelRequested(request_key, reason), self, aware(now)
        )
        self._write(job, decision.row, now, reason, False)
        return decision.row

    def cancel_race(self, job: Job, now: datetime) -> Lifecycle:
        """A build completed while its cancel is in flight and its cleanup is not
        confirmed: the cancel stays in flight (``running``) for the cleanup sweeper,
        which an operator never has to start (audit C7)."""

        before = self.lifecycle(job, issued=True, now=now)
        if before.terminal:
            return before
        flagged = (
            before
            if before.cancel_requested
            else replace(before, cancel_requested_at=aware(now))
        )
        decision = transition(flagged, Observed(Effect.UNKNOWN), self, aware(now))
        self._write(job, decision.row, now, None, True)
        return decision.row

    def heal(self, job: Job, now: datetime) -> bool:
        """A legacy ``waiting-for-operator`` parent that is cancelling is shown as
        ``running`` again; the sweeper (or its orders) completes the cancel."""

        if job.state != WAITING or not cancel_flagged(job):
            return False
        before = self.lifecycle(job, issued=True, now=now)
        self._write(job, replace(before, state=State.OBSERVING), now, None, True)
        return True


__all__ = ["KIND", "RecipeOperationAdapter", "cancel_flagged"]
