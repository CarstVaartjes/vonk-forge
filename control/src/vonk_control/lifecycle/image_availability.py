"""The lifecycle adapter of recipe image availability (``Job`` rows).

``recipe_image_availability`` keeps what is specific to preparing and removing a
recipe's runtime image and model files: the claim, the builder, the model child,
the removal fences and checkpoints, the failure evidence.  This module owns the
rest.  It is the **only** writer of the ``state`` of an availability preparation
(``recipe.image.availability.v2``), of its update-batch parent
(``recipe.cache.update.v2``, whose cancel it records) and of a recipe cache
removal (``recipe.cache.remove.v2``), and of the one-shot prebuilt-image import
(:class:`PrebuiltImageAdapter`).

Stored vocabulary is unchanged (the API, the worker's selects and the batch parent
read it):

====================  =========================================================
core state            stored job
====================  =========================================================
``queued``            ``queued``
``backoff``           ``queued`` (a preparation's failure, claimable once
                      ``payload.retry_after_at`` has passed) or ``partial`` (a
                      wait for the model child, an interrupted removal)
``running``           ``running`` with the owner's claim in the payload
                      (``claim_owner``, ``claim_until`` is the lease); a removal
                      has no lease of its own: a stored ``running`` removal is
                      between two fenced steps, so its lease is one step long
``observing``         ``cancelling`` (only a cancel observes)
``needs-operator``    never written: nothing here has an operator action; a
                      legacy row that maps here is retried on its first decision
``succeeded`` etc.    ``succeeded`` / ``failed`` / ``cancelled``
====================  =========================================================

Nothing is irreversible: a preparation resumes from its content-addressed receipts
and its exact children, a removal repeats its fenced step.  So rule 1 retries every
uncertain outcome and no row waits for an operator.  A failure the owner types as
terminal (an invalid recipe, a withdrawn revision, a security refusal) is a
definite ``failed``, as before.

A cancel always completes (rule 4).  A preparation that never ran and holds no
child is ``cancelled`` at once.  Otherwise it is ``cancelling`` while the owner
releases its claim and cancels its children, and it ends ``cancelled`` as soon as
nothing is outstanding or, when something stays outstanding, once the cancel has
been pending for the authority an agent cancellation is given
(``SUPERSEDED_CANCELLATION_SECONDS``) with the effect recorded as unknown.  The
budget is wall-clock from ``cancellation.cancel_requested_at`` (as for a profile
load): ``observe_count`` is derived from it in :meth:`lifecycle`.

One retry clock.  The core's bounded jittered backoff decides when a failed
preparation or removal is tried again; the owner's own delay (a provider's
``Retry-After``) is only the floor.  ``payload.retry_after_at`` (preparation) and
``checkpoint.failure.retry_time`` (removal) store the instant the core chose.

Legacy rows need no startup rewrite: ``lifecycle`` is the ``adopt`` hook, a
``running`` preparation with a lapsed lease is already claimable (the owner's claim
path re-claims it), a ``cancelling`` row with no clock gets the budget from its
recorded request, and a stored ``waiting-for-operator`` is retried.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from vonk_agent_protocol import LifecycleState

from .. import job_states
from ..agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS, aware
from ..models import Job
from ..recipe_image_availability_clocks_contract import StoredAvailabilityClocks
from ..recipe_image_removal_contract import RECIPE_CACHE_REMOVE_KIND
from ..recipe_update_contract import UPDATE_KIND
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .adapter import Dispatch
from .core import STOP_BUDGET, transition
from .types import (
    TERMINAL_STATES,
    CancelRequested,
    Claimed,
    Decision,
    Effect,
    Event,
    Heartbeat,
    Lifecycle,
    Observed,
    Outcome,
    Reported,
    State,
    StopResult,
)

if TYPE_CHECKING:
    from ..job_documents import AvailabilityJobPayload

KIND = "image-availability"
OPERATION_KIND = "recipe.image.availability.v2"
REMOVAL_KIND = RECIPE_CACHE_REMOVE_KIND
WAITING = LifecycleState.NEEDS_OPERATOR.value
CANCEL_BUDGET = timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS)
#: How long a stored ``running`` removal is trusted to be between two steps.
REMOVAL_STEP_LEASE = timedelta(minutes=2)
_MAX_REASON = 1024


class _Keep:
    pass


KEEP = _Keep()

_STORED = {
    State.QUEUED: State.QUEUED.value,
    State.RUNNING: State.RUNNING.value,
    State.OBSERVING: State.OBSERVING.value,
    State.NEEDS_OPERATOR: WAITING,
    State.SUCCEEDED: State.SUCCEEDED.value,
    State.FAILED: State.FAILED.value,
    State.CANCELLED: State.CANCELLED.value,
}


class ImageAvailabilityAdapter:
    """``KindAdapter`` for a preparation, an update batch's cancel and a removal."""

    kind = KIND

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return aware(self._clock())

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: Job) -> Lifecycle:
        return self.lifecycle(stored, self.now())

    def lifecycle(self, job: Job, now: datetime) -> Lifecycle:
        """The lifecycle row of a stored (possibly legacy) job."""

        now = aware(now)
        clocks = StoredAvailabilityClocks.read(job.payload)
        removal = job.kind == REMOVAL_KIND
        cancellation = clocks.cancellation
        requested_at = cancellation.cancel_requested_at if cancellation else None
        request_key = cancellation.cancel_request_id if cancellation else None
        checkpoint = clocks.checkpoint
        failure = checkpoint.failure if checkpoint else None
        retry_count = (
            ((checkpoint.retry_attempts or 0) if checkpoint else 0)
            if removal
            else ((clocks.retry.automatic_attempts or 0) if clocks.retry else 0)
        )
        stored = job.state
        due = (
            (failure.retry_time if failure else None)
            if removal
            else clocks.retry_after_at
        )
        lease: datetime | None = None
        fence = clocks.claim_owner
        issued = (
            job.kind == UPDATE_KIND
            or (int(job.current_attempt or 0) > 0 and not removal)
            or any(
                (
                    clocks.claim_owner,
                    clocks.image_reference_intent,
                    clocks.model_child,
                    clocks.build_dependency,
                )
            )
        )
        if stored == State.QUEUED:
            state = State.BACKOFF if due is not None and due > now else State.QUEUED
        elif job_states.means(stored, State.BACKOFF):
            state = State.BACKOFF
        elif stored == State.RUNNING:
            lease = (
                job.updated_at + REMOVAL_STEP_LEASE if removal else clocks.claim_until
            )
            lease = aware(lease) if lease is not None else None
            if lease is not None and lease > now:
                state = State.RUNNING
            else:
                # A lapsed (or missing) lease is claimable again: the claim path
                # re-claims it, which is the retry of idempotent work.
                state = State.BACKOFF
                due = lease if lease is not None else due
        elif job_states.means(stored, State.OBSERVING):
            state = State.OBSERVING
        elif stored == State.SUCCEEDED:
            state = State.SUCCEEDED
        elif stored == State.FAILED:
            state = State.FAILED
        elif stored == State.CANCELLED:
            state = State.CANCELLED
        else:  # waiting-for-operator or an unknown state: re-evaluate it
            state = State.NEEDS_OPERATOR
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        else:
            effect = Effect.ISSUED if issued else Effect.NONE
        spent = (
            requested_at is not None
            and state is State.OBSERVING
            and now >= requested_at + CANCEL_BUDGET
        )
        return Lifecycle(
            id=job.id,
            kind=KIND,
            state=state,
            # A batch has children of its own to stop whatever its attempt count.
            attempt=(
                retry_count + 1
                if removal
                else max(int(job.current_attempt or 0), int(issued and not removal))
            ),
            fence=fence,
            lease_deadline=lease if state is State.RUNNING else None,
            next_action_at=None if state in TERMINAL_STATES else due,
            retry_count=retry_count,
            observe_count=STOP_BUDGET if spent else 0,
            cancel_requested_at=requested_at,
            cancel_request_key=request_key,
            effect=effect,
            reason=job.status_reason,
        )

    # ------------------------------------------------------- kind questions

    def irreversible(self, row: Lifecycle) -> bool:
        return False  # receipts and fenced steps make every repeat safe

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        return Dispatch()  # the owner's worker claims its own work

    def observe(self, row: Lifecycle) -> Observed:
        return Observed(Effect.UNKNOWN)  # the owner's cancel pass is the inspection

    def stop(self, row: Lifecycle) -> StopResult:
        return StopResult.UNCONFIRMED  # likewise: the owner stops its children

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        return ()  # nothing here waits for a person

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()

    # ---------------------------------------------------------------- apply

    def apply(
        self,
        job: Job,
        after: Lifecycle,
        now: datetime,
        *,
        reason: str | None | _Keep = KEEP,
        visible: str | None = None,
        payload: AvailabilityJobPayload | None = None,
    ) -> AvailabilityJobPayload | None:
        """Project a core decision onto the stored job; the only such writer.

        Return the updated typed payload; the caller stores it through its contract.
        """

        from ..job_documents import AvailabilityJobPayload

        now = aware(now)
        if after.state is State.BACKOFF:
            state = visible or (
                State.BACKOFF.value if job.kind == REMOVAL_KIND else State.QUEUED.value
            )
        elif after.state is State.QUEUED and job.kind == REMOVAL_KIND:
            state = visible or (
                State.QUEUED.value if job.state == State.QUEUED else State.RUNNING.value
            )
        else:
            state = _STORED[after.state]
        job.state = state
        if not isinstance(reason, _Keep):
            job.status_reason = None if reason is None else reason[:_MAX_REASON]
        elif after.state is State.CANCELLED and after.effect is Effect.UNKNOWN:
            job.status_reason = (after.reason or "")[:_MAX_REASON] or None
        if (
            payload is not None
            and job.kind != REMOVAL_KIND
            and after.state is State.BACKOFF
            and after.next_action_at is not None
        ):
            payload = payload.model_copy(
                update={"retry_after_at": aware(after.next_action_at)}
            )
        if after.state is State.CANCELLED and after.effect is Effect.UNKNOWN:
            # The claim is fenced by the ended state; its keys no longer mean a
            # live lease (the exact children stay recorded as the residue).
            target = payload if payload is not None else read_row_column(job, "payload")
            if isinstance(target, AvailabilityJobPayload):
                target = target.model_copy(
                    update={"claim_owner": None, "claim_until": None}
                )
                if payload is None:
                    job.payload = serialize_json_value(target)
                else:
                    payload = target
        if job.kind != REMOVAL_KIND:
            job.current_attempt = max(int(job.current_attempt or 0), after.attempt)
        job.updated_at = now
        return payload

    def _drive(
        self,
        job: Job,
        event: Event,
        now: datetime,
        *,
        row: Lifecycle | None = None,
        reason: str | None | _Keep = KEEP,
        visible: str | None = None,
    ) -> Decision:
        """Feed ``event`` to the core and write what it decides, inline.

        The caller holds the job's lock.  A decision that leaves the row alone
        writes nothing.
        """

        now = aware(now)
        before = row if row is not None else self.lifecycle(job, now)
        decision = transition(before, event, self, now)
        if decision.row != before:
            self.apply(job, decision.row, now, reason=reason, visible=visible)
        return decision

    # ------------------------------------------------ the owner's own events

    def claim(
        self, job: Job, owner: str, lease_until: datetime, now: datetime
    ) -> Lifecycle:
        """The worker claims the preparation: a new attempt under a lease."""

        now = aware(now)
        row = self.lifecycle(job, now)
        if row.state is State.RUNNING:
            row = replace(row, state=State.BACKOFF, next_action_at=None)
        elif row.state in {State.BACKOFF, State.NEEDS_OPERATOR}:
            # The owner established that it is due (the retry clock, the model
            # poll); a legacy parked row is retried.
            row = replace(row, state=State.BACKOFF, next_action_at=None)
        decision = self._drive(
            job,
            Claimed(row.attempt + 1, owner, aware(lease_until)),
            now,
            row=row,
        )
        return decision.row

    def advance_removal(self, job: Job, now: datetime) -> Lifecycle:
        """A fenced removal step was taken: the removal is running, the failure
        (if any) is cleared by the owner's checkpoint."""

        now = aware(now)
        row = self.lifecycle(job, now)
        lease = now + REMOVAL_STEP_LEASE
        if row.state is State.RUNNING:
            event: Event = Heartbeat(None, lease)
        else:
            row = replace(row, state=State.BACKOFF, next_action_at=None)
            event = Claimed(row.attempt + 1, "removal", lease)
        return self._drive(job, event, now, row=row, reason=None).row

    def succeed(self, job: Job, now: datetime) -> Lifecycle:
        return self._drive(
            job,
            Reported(Outcome.DONE, reason=None),
            now,
            reason=None,
        ).row

    def plan_failure(
        self,
        job: Job,
        now: datetime,
        *,
        retryable: bool,
        retry_after: datetime | None = None,
        count: int | None = None,
    ) -> Lifecycle:
        """The core's decision for a failure the owner has classified (not written).

        A retryable one is retried (rule 1) at the core's bounded backoff, never
        earlier than ``retry_after`` (a provider's ``Retry-After``); a definite
        one ends.  ``count`` is the owner's failure count so far: a dependency
        wait is not a failure and does not advance the backoff.
        """

        now = aware(now)
        row = self.lifecycle(job, now)
        if count is not None:
            row = replace(row, retry_count=count)
        if row.state in {State.BACKOFF, State.QUEUED, State.NEEDS_OPERATOR}:
            # The owner's lock and claim established that the report is current
            # (its lease may have lapsed while it ran).
            row = replace(row, state=State.RUNNING, fence=None, next_action_at=None)
        return transition(
            row,
            Reported(Outcome.FAILED, retryable=retryable, retry_after=retry_after),
            self,
            now,
        ).row

    def commit(
        self,
        job: Job,
        after: Lifecycle,
        now: datetime,
        *,
        reason: str | None | _Keep = KEEP,
        payload: AvailabilityJobPayload | None = None,
    ) -> AvailabilityJobPayload | None:
        """Write a decision :meth:`plan_failure` returned."""

        return self.apply(job, after, now, reason=reason, payload=payload)

    def fail(
        self,
        job: Job,
        now: datetime,
        *,
        retryable: bool,
        reason: str,
        retry_after: datetime | None = None,
        count: int | None = None,
    ) -> Lifecycle:
        after = self.plan_failure(
            job, now, retryable=retryable, retry_after=retry_after, count=count
        )
        self.commit(job, after, now, reason=reason)
        return after

    def defer(
        self,
        job: Job,
        now: datetime,
        retry_after: datetime,
        *,
        payload: AvailabilityJobPayload,
    ) -> AvailabilityJobPayload:
        """A dependency wait returns its typed payload with the core retry clock."""
        now = aware(now)
        row = self.lifecycle(job, now)
        decision = transition(
            row, Reported(Outcome.UNKNOWN, retry_after=retry_after), self, now
        )
        updated = self.apply(
            job, decision.row, now, visible=State.BACKOFF.value, payload=payload
        )
        assert updated is not None
        return updated

    def request_cancel(
        self, job: Job, request_key: str | None, reason: str, now: datetime
    ) -> Lifecycle:
        """Rule 4: work that never ran ends at once, anything else is stopped and
        observed (``cancelling``) until nothing is outstanding or the budget ends."""

        return self._drive(
            job, CancelRequested(request_key, reason), now, reason=reason
        ).row

    def supersede(self, job: Job, reason: str, now: datetime) -> Lifecycle:
        """Rule 6: a newer intent cancels an older, not-yet-started preparation."""

        return self._drive(job, CancelRequested(None, reason), now, reason=reason).row

    def settle_cancel(self, job: Job, now: datetime, *, outstanding: bool) -> Lifecycle:
        """The owner's cancel pass: ``outstanding`` is whether a claim, a child or
        a fence is still to be released.  Nothing outstanding ends the cancel; the
        budget ends it with the effect unknown; otherwise the row keeps observing."""

        now = aware(now)
        row = self.lifecycle(job, now)
        if row.state is not State.OBSERVING or not row.cancel_requested:
            return row
        answer = Observed(Effect.UNKNOWN if outstanding else Effect.STOPPED)
        return self._drive(job, answer, now, row=row).row

    @staticmethod
    def new_job(*, state: str = State.QUEUED.value, **fields: Any) -> Job:
        """A new job, ``queued`` and claimable now."""

        return Job(state=state, **fields)


class PrebuiltImageAdapter:
    """The one-shot import of a catalog's prebuilt runtime image (a build ``Job``).

    The import is a single idempotent pull into the Controller's image store, so
    its end is definite either way: a pulled image is ``succeeded`` and a failed
    pull is a ``failed`` build whose plan builds on a Spark instead and tries the
    pull again after an hour (the owner's block, not a retry of this job).
    """

    kind = "prebuilt-image"

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def adopt(self, stored: Job) -> Lifecycle:
        """A claimed import is running; an ended one is its end."""

        state = {
            State.SUCCEEDED.value: State.SUCCEEDED,
            State.FAILED.value: State.FAILED,
            State.CANCELLED.value: State.CANCELLED,
        }.get(stored.state, State.RUNNING)
        return Lifecycle(
            id=stored.id,
            kind=self.kind,
            state=state,
            attempt=max(int(stored.current_attempt or 0), 1),
            effect=Effect.ISSUED,
        )

    def finish(self, job: Job, *, ok: bool, now: datetime) -> Lifecycle:
        now = aware(now)
        row = self.adopt(job)
        decision = transition(
            row,
            Reported(Outcome.DONE if ok else Outcome.FAILED, retryable=False),
            self,
            now,
        )
        job.state = _STORED[decision.row.state]
        job.updated_at = now
        return decision.row

    def irreversible(self, row: Lifecycle) -> bool:
        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        return Dispatch()

    def observe(self, row: Lifecycle) -> Observed:
        return Observed(Effect.UNKNOWN)

    def stop(self, row: Lifecycle) -> StopResult:
        return StopResult.UNCONFIRMED

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()


__all__ = [
    "CANCEL_BUDGET",
    "KEEP",
    "KIND",
    "ImageAvailabilityAdapter",
    "PrebuiltImageAdapter",
]
