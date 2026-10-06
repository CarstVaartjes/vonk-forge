"""The lifecycle adapter of a recipe update batch (``Job`` of kind ``recipe.cache.update.v2``).

A batch is a **composite** over its frozen children (one recipe-revision
availability operation each).  It has no effect of its own: the children's own
operations carry the effect, the retry and the cancel, so a batch is never
irreversible, never executes anything itself and never waits for an operator.
``recipe_update_batches`` keeps the document, the claim fence, admission and the
views; this module is the **only** writer of the batch's ``Job.state``.

========================  =====================================================
core state                stored batch
========================  =====================================================
``queued``                ``queued``
``running``               ``running`` (a worker holds the claim)
``backoff``               ``queued`` / ``running`` until ``next_attempt_at``
``observing`` + cancel    ``cancelling``
``needs-operator``        never written (no batch action exists): legacy only
``succeeded``             ``succeeded``
``failed``                ``failed``, or ``partial`` when some children succeeded
``cancelled``             ``cancelled``
========================  =====================================================

A cancel always completes.  Children are cancelled and observed by the owner; the
batch ends ``cancelled`` as soon as every child has settled, or, when one cannot
be confirmed within the 660 s an agent cancellation is already given, ends
``cancelled`` with the children's effect recorded as unknown (the child
identities stay in the document as the residue) instead of staying ``cancelling``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import job_states
from ..agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS, aware
from ..models import Job
from ..recipe_update_contract import RecipeUpdateChild, RecipeUpdateDocument
from .adapter import Dispatch
from .composite import aggregate
from .core import STOP_BUDGET, transition
from .reconciler import Settled
from .reconciler import settle as settle_event
from .types import (
    TERMINAL_STATES,
    Claimed,
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

KIND = "recipe.cache.update.v2"
_MAX_REASON = 512
CANCEL_BUDGET = timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS)
KEEP: Any = object()
_STORED = {
    State.QUEUED: "queued",
    State.RUNNING: "running",
    State.BACKOFF: "queued",
    State.OBSERVING: "running",
    State.NEEDS_OPERATOR: "queued",  # never written as a wait: it is healed
    State.SUCCEEDED: "succeeded",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
}
#: A child that has not been issued yet backs off (it will be); every other child
#: speaks the core vocabulary, an old spelling adopted by its contract type.
_PENDING_CHILD = "pending"
_CANCEL_EFFECT_UNKNOWN = (
    "recipe-update.cancel-effect-unknown: a child operation was still being "
    "cancelled when the stop budget ended; its own lifecycle owns it"
)


def child_lifecycle(child: RecipeUpdateChild) -> Lifecycle:
    """A child's recorded observation as a lifecycle row."""

    state = State.BACKOFF if child.state == _PENDING_CHILD else State(child.state)
    if state is State.SUCCEEDED:
        effect = Effect.ESTABLISHED
    elif state is State.CANCELLED:
        effect = Effect.STOPPED
    else:
        effect = Effect.ISSUED if child.operation_id is not None else Effect.NONE
    return Lifecycle(
        id=child.request_key,
        kind=KIND,
        state=state,
        attempt=1 if child.operation_id is not None else 0,
        next_action_at=None if state in TERMINAL_STATES else child.retry_at,
        effect=effect,
    )


class RecipeUpdateBatchAdapter:
    """``KindAdapter`` for a stored recipe update batch."""

    kind = KIND

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: Job) -> Lifecycle:
        return self.lifecycle(stored, None, datetime.now(UTC))

    def lifecycle(
        self, job: Job, document: RecipeUpdateDocument | None, now: datetime
    ) -> Lifecycle:
        """The lifecycle row of a stored (possibly legacy) batch.

        A document that cannot be read still maps (to the stored state, without a
        clock): the core never raises for it, and the owner reports the damage.  A
        legacy ``waiting-for-operator`` batch is ``needs-operator`` and is
        re-evaluated by the first decision (nothing advertises an action).
        """

        now = aware(now)
        stored = job.state
        due: datetime | None = None
        lease: datetime | None = None
        fence: str | None = None
        if document is not None:
            due = (
                None
                if document.next_attempt_at is None
                else aware(document.next_attempt_at)
            )
            if document.claim_until is not None and aware(document.claim_until) > now:
                lease = aware(document.claim_until)
                fence = document.claim_owner
        if stored == "queued":
            state = State.BACKOFF if due is not None and due > now else State.QUEUED
        elif stored == "running":
            if lease is not None:
                state = State.RUNNING
            else:
                state = State.BACKOFF if due is not None and due > now else State.QUEUED
        elif job_states.means(stored, State.OBSERVING, kind=KIND):
            state = State.OBSERVING
        elif stored == "succeeded":
            state = State.SUCCEEDED
        elif stored in job_states.words(State.FAILED, kind=KIND):
            state = State.FAILED
        elif stored == "cancelled":
            state = State.CANCELLED
        else:  # waiting-for-operator, or a state this adapter does not know
            state = State.NEEDS_OPERATOR
        issued = job.current_attempt > 0 or (
            document is not None
            and any(child.operation_id is not None for child in document.children)
        )
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        else:
            effect = Effect.ISSUED if issued else Effect.NONE
        requested_at: datetime | None = None
        request_key: str | None = None
        observe_count = 0
        cancellation = None if document is None else document.cancellation
        if cancellation is not None:
            requested_at = aware(cancellation.cancel_requested_at or job.updated_at)
            request_key = cancellation.cancel_request_id
            observe_count = STOP_BUDGET if now >= requested_at + CANCEL_BUDGET else 0
        elif job_states.means(stored, State.OBSERVING, kind=KIND):
            # A cancelling batch whose request cannot be read: the request is
            # still a request (monotonic); see :meth:`cancel_unreadable`.
            requested_at = aware(job.updated_at)
        return Lifecycle(
            id=job.id,
            kind=KIND,
            state=state,
            attempt=job.current_attempt,
            fence=fence,
            lease_deadline=lease,
            # A cancel is paced by the owner's pass, not by the batch's clock.
            next_action_at=(
                None if state in TERMINAL_STATES or requested_at is not None else due
            ),
            observe_count=observe_count,
            cancel_requested_at=requested_at,
            cancel_request_key=request_key,
            effect=effect,
            reason=job.status_reason,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """A composite has no effect of its own: its children own theirs."""

        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The claim and the child passes are the owner's, not the core's."""

        return Dispatch(issued=False)

    def observe(self, row: Lifecycle) -> Observed:
        return Observed(Effect.UNKNOWN, "the batch follows its children")

    def stop(self, row: Lifecycle) -> StopResult:
        """The children are cancelled by the owner's pass; unconfirmed here."""

        return StopResult.UNCONFIRMED

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """None: a batch waits for no person; its Stop is Cancel."""

        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()  # read from the document, see :meth:`children_of`

    @staticmethod
    def children_of(document: RecipeUpdateDocument) -> tuple[Lifecycle, ...]:
        return tuple(child_lifecycle(child) for child in document.children)

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        job: Job,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
        *,
        reason: str | None = KEEP,
        partial: bool = False,
        visible: str | None = None,
    ) -> None:
        """Project a core decision onto the stored batch; the only such writer."""

        # A cancel in flight is ``observing`` and a batch that ended with some
        # children done is ``failed`` (its view says ``partial``; it is derived
        # from the children, not stored).
        if after.state is State.OBSERVING and after.cancel_requested:
            state = State.OBSERVING.value
        elif visible is not None and not after.terminal:
            state = visible
        else:
            state = _STORED[after.state]
        job.state = state
        if reason is not KEEP:
            job.status_reason = None if reason is None else reason[:_MAX_REASON]
        job.updated_at = aware(now)

    def settled(
        self,
        job: Job,
        document: RecipeUpdateDocument | None,
        event: Event,
        now: datetime,
        *,
        unflag: bool = False,
        **write: Any,
    ) -> Settled:
        now = aware(now)
        row = self.lifecycle(job, document, now)
        if unflag:
            row = replace(row, cancel_requested_at=None, cancel_request_key=None)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            options = dict(write)
            if after.state is State.CANCELLED and after.effect is Effect.UNKNOWN:
                base = options.get("reason", KEEP)
                text = job.status_reason if base is KEEP else base
                options["reason"] = (
                    f"{text}; {_CANCEL_EFFECT_UNKNOWN}"
                    if text
                    else _CANCEL_EFFECT_UNKNOWN
                )
            self.apply(job, before, after, now, **options)
            return True

        return settle_event(
            row, event, self, now, save=save, residue=lambda _row, _why: None
        )

    def decide(
        self,
        job: Job,
        document: RecipeUpdateDocument | None,
        event: Event,
        now: datetime,
    ) -> Decision:
        now = aware(now)
        return transition(self.lifecycle(job, document, now), event, self, now)

    # ------------------------------------------------- the batch's events

    def claimed(
        self,
        job: Job,
        document: RecipeUpdateDocument,
        owner: str,
        until: datetime,
        now: datetime,
    ) -> Lifecycle:
        """A worker took the batch: a new attempt under a new fence and lease.

        A batch whose next pass is not yet due, that is already claimed or that asked
        for a cancel is not claimable: the core absorbs the event and the row stays.
        """

        now = aware(now)
        settled = self.settled(
            job,
            document,
            Claimed(attempt=job.current_attempt + 1, fence=owner, lease_deadline=until),
            now,
        )
        if settled.row.state is State.RUNNING:
            job.current_attempt = settled.row.attempt
        return settled.row

    def conclude(
        self, job: Job, document: RecipeUpdateDocument, now: datetime
    ) -> Lifecycle | None:
        """Every child settled: the batch ends (worst outcome wins); ``None`` while
        any child has not."""

        children = self.children_of(document)
        state = aggregate(children)
        if state not in TERMINAL_STATES:
            return None
        if state is State.SUCCEEDED:
            return self.settled(job, document, Reported(Outcome.DONE), now).row
        partial = any(child.state is State.SUCCEEDED for child in children)
        return self.settled(
            job,
            document,
            Reported(Outcome.FAILED, retryable=False),
            now,
            partial=partial,
        ).row

    def project(
        self,
        job: Job,
        document: RecipeUpdateDocument,
        now: datetime,
        *,
        visible: str,
    ) -> Lifecycle:
        """Show what the children are doing (a projection, not a decision)."""

        now = aware(now)
        before = self.lifecycle(job, document, now)
        if before.terminal:
            return before
        state = State.RUNNING if visible == "running" else State.QUEUED
        after = replace(before, state=state)
        self.apply(job, before, after, now, visible=visible)
        return after

    def reject(self, job: Job, reason: str, now: datetime) -> Lifecycle:
        """Retain a batch whose stored document cannot be read (never repaired).

        The document is the evidence of what was admitted, so it is kept as it is;
        the batch ends ``failed`` with the reason and stops being advanced.
        """

        return self.settled(
            job,
            None,
            Reported(Outcome.FAILED, retryable=False, reason=reason),
            now,
            reason=reason,
        ).row

    def cancel_progress(
        self,
        job: Job,
        document: RecipeUpdateDocument | None,
        now: datetime,
        *,
        settled_children: bool,
        reason: str | None,
    ) -> Lifecycle:
        """One pass of a cancel in flight (rule 4): every child settled ends it
        ``cancelled``; otherwise the stop is unconfirmed, and the budget ends it with
        the effect unknown.  A cancel never waits."""

        event: Event = Observed(Effect.STOPPED) if settled_children else Tick()
        return self.settled(job, document, event, now, reason=reason).row

    def cancel_unreadable(self, job: Job, now: datetime) -> Lifecycle:
        """A cancel whose document cannot be read ends ``cancelled`` with the
        children's effect unknown; the document is retained untouched as the residue."""

        return self.settled(
            job,
            None,
            Reported(
                Outcome.CANCELLED,
                effect=Effect.UNKNOWN,
                reason="stored update document cannot be read",
            ),
            now,
            reason=job.status_reason or "cancelled",
        ).row

    @staticmethod
    def new_batch(*, allowed: bool, **fields: Any) -> Job:
        """A new batch: ``queued``, or ``succeeded`` when it has nothing to update."""

        return Job(state="queued" if allowed else "succeeded", **fields)


__all__ = [
    "CANCEL_BUDGET",
    "KEEP",
    "KIND",
    "RecipeUpdateBatchAdapter",
    "child_lifecycle",
]
