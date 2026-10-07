"""The lifecycle adapter of the Spark order (``AgentOperation``).

``agent_jobs`` keeps what is specific to the queue: SQL, locking, the claim
predicate, authentication and fencing, protocol validation.  This module owns the
rest: how a stored order *is* a lifecycle row (:meth:`AgentOperationAdapter.lifecycle`,
the ``adopt`` hook), what the kind knows that the core asks (irreversible, the
retry fence, the operator actions, a stop), and the **only** projection of a core
decision back onto the stored rows (:meth:`AgentOperationAdapter.apply`).  No
other code writes an order's ``state`` for a recovery decision.

Stored encoding.  An order's stored ``state`` is a word of the core vocabulary
(``vonk_agent_protocol.LifecycleState``), so each core state is stored as itself:

========================  ==========================================
core state                stored order
========================  ==========================================
``queued``                ``queued`` (attempt 0)
``running``               ``running``
``backoff``               ``backoff`` + ``next_action_at`` (the retry clock)
``observing``             ``observing`` + ``next_action_at``, ``observe_count`` > 0
``needs-operator``        ``needs-operator``, ``next_action_at`` NULL
``succeeded``/``failed``  ``succeeded``/``failed``
``cancelled``             ``cancelled``
========================  ==========================================

A row written before the rename says ``waiting-for-operator`` for all three waits;
the contract adopts it (``agent_operation_states.PARKED`` selects both spellings)
and the schedule columns still say which wait it was, so the reconciler classifies it
on its first pass.  An attempt ends ``succeeded``, ``failed``, ``cancelled`` or
``observing`` with a typed ``observation_cause`` (``reported-unknown`` or
``lease-lapsed``); the agent wire keeps its own four result words and is mapped at
ingress (``agent_operation_states.record_wire_state``).

A parked order with ``next_action_at`` set is an automatic retry (an operator is not
involved); without it the order waits, and the
reconciler re-evaluates it on every pass (rules 1 to 3), so a wait never outlives
its cause and an observation needs no persisted counter.

What ``effect`` means for an order: ``issued`` while an attempt holds a live lease;
``none`` when no attempt does and the kind is restart-safe (the exact-resume path
re-establishes whatever the old attempt left); ``unknown`` for an irreversible kind
whose attempt ended without a receipt.

Only ``recipe.job.run.v1`` is irreversible (a user's job may have run).
Builds and build cleanup are rebuildable and retry automatically.  An
agent upgrade is *not* irreversible here: its claim already refuses to install over
any binary other than the exact rollback source, so a retry behind the dpkg safety
fence cannot repeat a visible effect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentOperation,
    InvalidRequestReason,
    LifecycleState,
    LifecycleSubject,
    OperationProgress,
    adopt_state,
    canonical_message,
)
from vonk_agent_protocol.contracts import AgentFailureResult, AgentResultPayload

from .. import agent_operation_states as aos
from .. import job_states
from ..agent_operation_facts import (
    AGENT_UPGRADE_RECOVERY_FENCE,
    RESTART_REISSUE_OPERATIONS,
    STALLED_INTERRUPTION_LIMIT,
    STALLED_RETRY_BASE_SECONDS,
    STALLED_RETRY_MAX_SECONDS,
    attempt_holds_open_launch_budget,
    attempt_is_live,
    aware,
    operation_start_deadline,
    stalled_interruptions,
)
from ..categorized_errors import InvalidValue
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job
from ..operation_progress import progress_document
from .adapter import Dispatch
from .core import OBSERVE_BUDGET, transition
from .reconciler import settle
from .types import (
    ActionName,
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
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

#: The kinds whose repeat could repeat a visible effect (see the module docstring).
IRREVERSIBLE_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_JOB_RUN.value,
    }
)
#: The word of a parent *job* that waits (the job table converts with its own kind).
WAITING = LifecycleState.NEEDS_OPERATOR.value
JOB_RUN_OPERATION = AgentOperation.RECIPE_JOB_RUN.value
#: The owner kind a one-shot job's order carries in its parent job's payload.
OWNER_KIND = "artifact-job"
#: The one operator action of a one-shot job's order (the artifact job owns it).
STOP_ACTION = ActionName.STOP.value
#: A parent in one of these states can no longer claim, resume or retire anything:
#: its orders end with it instead of waiting.
ENDED_PARENT_STATES = frozenset(
    job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
)
#: States in which a parent's aggregate considers an order finished (a waiting
#: order is final for the aggregate: the parent then waits with it).
AGGREGATE_FINAL_STATES = frozenset(
    {"cancelled", aos.RETAINED_COMPENSATED, "failed", "succeeded", aos.NEEDS_OPERATOR}
)
#: The operator actions of a parked order (the same pair the Job endpoints take).
OPERATOR_ACTIONS = (ActionName.RESUME.value, ActionName.RETIRE.value)
_LEGACY_RETRY = ActionName.RETRY.value
_MAX_REASON = 512

ResumeCandidates = Callable[[Session, str, datetime], Sequence[StoredOperation]]


def is_artifact_owned(parent: Job | None) -> bool:
    """Whether a parent job is the submission of a one-shot artifact job.

    Such an order is owned by its artifact job: its operator action is ``stop``
    only, never ``resume`` or ``retire`` (a resume would run a user's job again).
    """

    payload = None if parent is None else parent.payload
    return (
        parent is not None
        and parent.kind == JOB_RUN_OPERATION
        and isinstance(payload, Mapping)
        and payload.get("owner_kind") == OWNER_KIND
    )


def _legacy_retry_due(operation: StoredOperation) -> datetime | None:
    """The retry instant of a row written before the core, else ``None``."""

    if (
        operation.retry_disposition == _LEGACY_RETRY
        and operation.retry_disposition_attempt == operation.current_attempt
    ):
        return aware(operation.retry_due_at or operation.updated_at)
    return None


def retry_scheduled(operation: StoredOperation) -> datetime | None:
    """When a waiting order's automatic retry becomes claimable, if it has one."""

    if not aos.order_is_parked(operation.state):
        return None
    if operation.next_action_at is not None:
        # Above zero the order is being observed, which is not a retry.
        return aware(operation.next_action_at) if not operation.observe_count else None
    return _legacy_retry_due(operation)


def cancel_requested_at(
    parent: Job | None, now: datetime
) -> tuple[datetime | None, str | None]:
    """The parent's monotonic cancel request: its instant and request id."""

    result = None if parent is None else parent.result
    if not isinstance(result, Mapping) or result.get("cancel_requested") is not True:
        return None, None
    requested: datetime | None = None
    value = result.get("cancel_requested_at")
    if isinstance(value, str):
        try:
            requested = datetime.fromisoformat(value)
        except ValueError:
            requested = None
    if requested is None or requested.tzinfo is None:
        requested = now  # a request that names no instant is still a request
    key = result.get("cancel_request_id")
    return aware(requested), key if isinstance(key, str) else None


class AgentOperationAdapter:
    """``KindAdapter`` for a stored Spark order, bound to a session or a factory.

    Inline (inside ``agent_jobs``' transaction) it is bound to that session so every
    read sees the rows being decided.  For the reconciler it is bound to a session
    factory and reads committed state in short sessions of its own.
    """

    kind = "agent-operation"

    def __init__(
        self,
        session: Session | None = None,
        *,
        sessions: sessionmaker[Session] | None = None,
        clock: Callable[[], datetime] | None = None,
        resume_candidates: ResumeCandidates | None = None,
    ) -> None:
        if (session is None) == (sessions is None):
            raise InvalidValue(
                "bind the adapter to a session or to a session factory",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        self._session = session
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._resume_candidates = resume_candidates

    # ------------------------------------------------------------- plumbing

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

    @staticmethod
    def attempt_of(
        session: Session, operation: StoredOperation
    ) -> AgentOperationAttempt | None:
        return session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: StoredOperation) -> Lifecycle:
        """Map a stored (possibly legacy) order onto the core; pure but for reads."""

        with self._read() as session:
            return self.lifecycle(
                stored,
                self.attempt_of(session, stored),
                session.get(Job, stored.parent_job_id),
                self.now(),
            )

    def lifecycle(
        self,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        parent: Job | None,
        now: datetime,
    ) -> Lifecycle:
        """The lifecycle row of a stored order, defaulting what a legacy row lacks."""

        now = aware(now)
        irreversible = operation.kind in IRREVERSIBLE_OPERATIONS
        lease: datetime | None = None
        next_action: datetime | None = None
        stored = operation.state
        if stored == "queued":
            state = State.QUEUED
        elif stored == "running":
            state = State.RUNNING
            if attempt is not None and attempt.state == "running":
                lease = aware(attempt.lease_deadline)
                deadline = operation_start_deadline(operation)
                if attempt_holds_open_launch_budget(operation, attempt, now):
                    assert deadline is not None
                    lease = max(lease, aware(deadline))
            next_action = lease
        elif stored in aos.PARKED:
            scheduled = retry_scheduled(operation)
            if scheduled is not None:
                state, next_action = State.BACKOFF, scheduled
            elif operation.next_action_at is not None:
                state, next_action = State.OBSERVING, aware(operation.next_action_at)
            else:
                state, next_action = State.NEEDS_OPERATOR, None
        elif stored == "succeeded":
            state = State.SUCCEEDED
        elif stored == "failed":
            state = State.FAILED
        elif stored == "cancelled":
            state = State.CANCELLED
        else:  # a state this adapter does not know: re-evaluate it as a wait
            state = State.NEEDS_OPERATOR
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            effect = Effect.STOPPED
        elif operation.current_attempt == 0:
            effect = Effect.NONE
        elif state is State.RUNNING:
            effect = Effect.ISSUED
        else:
            effect = Effect.UNKNOWN if irreversible else Effect.NONE
        requested_at, request_key = cancel_requested_at(parent, now)
        if (
            requested_at is None
            and parent is not None
            and parent.state in ENDED_PARENT_STATES
        ):
            # Its job ended without it: nothing can claim, resume or retire it, so
            # it is cancelled like any other order whose owner no longer wants it.
            requested_at = now
        return Lifecycle(
            id=operation.id,
            kind=operation.kind,
            state=state,
            attempt=operation.current_attempt,
            fence=None if attempt is None else attempt.fence,
            lease_deadline=lease,
            next_action_at=next_action,
            retry_count=max(operation.current_attempt - 1, 0),
            observe_count=operation.observe_count or 0,
            intent_ordinal=operation.workload_intent_ordinal,
            cancel_requested_at=requested_at,
            cancel_request_key=request_key,
            effect=effect,
            reason=operation.status_reason,
            owner_id=operation.parent_job_id,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        return row.kind in IRREVERSIBLE_OPERATIONS

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        """The agent-upgrade dpkg fence, and a crash-looping transfer's slowdown."""

        if row.kind == AgentOperation.AGENT_UPGRADE.value:
            return aware(now) + AGENT_UPGRADE_RECOVERY_FENCE
        if row.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value:
            with self._read() as session:
                operation = session.get(StoredOperation, row.id)
                stalled = 0 if operation is None else stalled_interruptions(operation)
            if stalled >= STALLED_INTERRUPTION_LIMIT:
                # Rate, not intent, is what a crash loop bounds: slow down to a
                # cap while the request stays authorised.
                delay = min(
                    STALLED_RETRY_BASE_SECONDS
                    * 2 ** (stalled - STALLED_INTERRUPTION_LIMIT),
                    STALLED_RETRY_MAX_SECONDS,
                )
                return aware(now) + timedelta(seconds=delay)
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The Spark claims its own work; a due retry is claimable as it is."""

        return Dispatch(issued=False)

    def observe(self, row: Lifecycle) -> Observed:
        """The Controller has no probe of a Spark order's effect.

        The Spark inspects before it re-issues (exact-resume), so the only
        observation that exists is the next attempt's own.
        """

        return Observed(Effect.UNKNOWN, "the effect is unobserved")

    def stop(self, row: Lifecycle) -> StopResult:
        """Idempotent.  Confirmed when the order has nothing left to stop.

        A live attempt is still running its effect: unconfirmed.  An irreversible
        order that ran leaves an effect the Controller cannot observe (a user's
        job): unconfirmed too, so a cancel of it ends after the stop budget with
        the effect recorded as unknown, never as stopped.
        """

        with self._read() as session:
            operation = session.get(StoredOperation, row.id)
            if operation is None:
                return StopResult.CONFIRMED
            live = attempt_is_live(
                operation, self.attempt_of(session, operation), self.now()
            )
            unobservable = (
                operation.kind in IRREVERSIBLE_OPERATIONS
                and operation.current_attempt > 0
            )
        return StopResult.UNCONFIRMED if live or unobservable else StopResult.CONFIRMED

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """``resume``/``retire`` exactly when the Job endpoints would accept them.

        An artifact job's order has ``stop`` instead, until a cancel is requested
        (then the cancel completes by itself and nothing is left to advertise).
        """

        if row.owner_id is None:
            return ()
        with self._read() as session:
            if is_artifact_owned(session.get(Job, row.owner_id)):
                return () if row.cancel_requested else (STOP_ACTION,)
            if self._resume_candidates is None:
                return ()
            candidates = self._resume_candidates(session, row.owner_id, self.now())
            if any(candidate.id == row.id for candidate in candidates):
                return OPERATOR_ACTIONS
        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
    ) -> bool:
        """Project a core decision onto the stored order; the only such writer.

        Returns whether anything changed.  A row that would be rewritten with the
        same values is left alone (no ``updated_at`` churn for a wait the
        reconciler re-evaluates on every pass).
        """

        now = aware(now)
        state, next_action = self._stored(operation, after, now)
        reason = after.reason
        if after.state is State.BACKOFF and operation.current_attempt == 0:
            reason = reason or "queued again; it never ran"
        elif after.state is State.BACKOFF and (
            before.state is not State.BACKOFF
            or after.next_action_at != before.next_action_at
        ):
            reason = self._scheduled_reason(operation, after, reason)
        reason = None if reason is None else reason[:_MAX_REASON]
        changed = False
        if (
            before.state is State.RUNNING
            and after.state not in {State.RUNNING, State.SUCCEEDED, State.FAILED}
            and attempt is not None
            and attempt.state == "running"
        ):
            aos.lapse(attempt)
            changed = True
        stored_next = (
            None
            if operation.next_action_at is None
            else aware(operation.next_action_at)
        )
        observed = after.observe_count if after.state is State.OBSERVING else 0
        if (
            operation.state != state
            or stored_next != next_action
            or (operation.observe_count or 0) != observed
            or (reason is not None and operation.status_reason != reason)
        ):
            operation.state = state
            operation.next_action_at = next_action
            operation.observe_count = observed
            if reason is not None:
                operation.status_reason = reason
            operation.retry_disposition = None
            operation.retry_disposition_attempt = None
            operation.retry_due_at = None
            operation.updated_at = now
            changed = True
        if attempt is not None and after.state is State.BACKOFF:
            changed |= self._fence_upgrade_attempt(operation, attempt, next_action)
        return changed

    @staticmethod
    def _stored(
        operation: StoredOperation, after: Lifecycle, now: datetime
    ) -> tuple[str, datetime | None]:
        match after.state:
            case State.QUEUED:
                if operation.current_attempt == 0:
                    return "queued", None
                # A queued retry is claimable now; only a waiting order is.
                return aos.BACKOFF, after.next_action_at or now
            case State.BACKOFF:
                if operation.current_attempt == 0:
                    # Never issued: it is simply queued, and a queued order
                    # (attempt 0) is what the claim predicate offers.
                    return "queued", None
                return aos.BACKOFF, after.next_action_at or now
            case State.OBSERVING:
                return aos.OBSERVING, after.next_action_at or now
            case State.NEEDS_OPERATOR:
                return aos.NEEDS_OPERATOR, None
            case State.RUNNING:
                return "running", None
            case State.SUCCEEDED:
                return "succeeded", None
            case State.FAILED:
                return "failed", None
            case State.CANCELLED:
                return "cancelled", None
        # A state the stored vocabulary has no word for is unknown, not an error:
        # the order is observed again and the core decides from what it finds.
        return aos.OBSERVING, after.next_action_at or now  # pragma: no cover

    def _scheduled_reason(
        self, operation: StoredOperation, after: Lifecycle, event_reason: str | None
    ) -> str:
        """What an operator reads for a scheduled retry."""

        due = after.next_action_at
        assert due is not None
        if operation.kind == AgentOperation.AGENT_UPGRADE.value:
            note = f"agent upgrade retries automatically after {due.isoformat()}"
        else:
            stalled = stalled_interruptions(operation)
            if stalled >= STALLED_INTERRUPTION_LIMIT:
                note = (
                    f"agent restarted {stalled} times in a row during "
                    f"{operation.kind} without copying new bytes; inspect the "
                    f"Spark's agent journal; retry scheduled at {due.isoformat()}"
                )
            else:
                note = (
                    f"exact {operation.kind} interrupted; retry scheduled at "
                    f"{due.isoformat()}"
                )
        return f"{event_reason}; {note}" if event_reason else note

    @staticmethod
    def _fence_upgrade_attempt(
        operation: StoredOperation,
        attempt: AgentOperationAttempt,
        not_before: datetime | None,
    ) -> bool:
        """The retried upgrade's attempt keeps its lease until the fence elapses.

        ``agent_upgrade_in_flight`` reads the attempt's lease as the moment the
        dpkg safety fence ends, so no other Spark installs while this one may
        still be.
        """

        if operation.kind != AgentOperation.AGENT_UPGRADE.value or not_before is None:
            return False
        changed = False
        if attempt.state == "running":
            aos.lapse(attempt)
            changed = True
        elif not (attempt.state == "failed" or aos.attempt_is_observing(attempt)):
            aos.record_wire_state(attempt, aos.WIRE_UNKNOWN)
            changed = True
        if aware(attempt.lease_deadline) < not_before:
            attempt.lease_deadline = not_before
            changed = True
        return changed

    # ---------------------------------------------------------------- settle

    def settle(
        self,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        parent: Job | None,
        event: Event,
        now: datetime,
        *,
        reason_suffix: str | None = None,
    ) -> Lifecycle:
        """Feed ``event`` to the core and write what it decides, inline.

        The caller holds the locks; ``operation`` and ``attempt`` are changed in
        place, so a command's follow-up read sees the new state.
        """

        now = aware(now)
        if attempt is None and operation.current_attempt > 0:
            attempt = self._attempt_of(operation)
        if parent is None:
            parent = self._parent_of(operation)
        row = self.lifecycle(operation, attempt, parent, now)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            self.apply(operation, attempt, before, after, now)
            return True

        def residue(row: Lifecycle, reason: str) -> None:
            operation.status_reason = reason[:_MAX_REASON]

        settled = settle(row, event, self, now, save=save, residue=residue)
        return settled.row

    def decide(
        self,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        parent: Job | None,
        event: Event,
        now: datetime,
    ) -> Decision:
        """The core's decision for ``event`` without writing it (for tests and probes)."""

        row = self.lifecycle(operation, attempt, parent, aware(now))
        return transition(row, event, self, aware(now))

    # ---------------------------------------------------------- claim writes

    def start_attempt(
        self,
        operation: StoredOperation,
        parent: Job | None,
        certificate_serial: str,
        fence: str,
        deadline: datetime,
        now: datetime,
        *,
        progress: OperationProgress | None,
    ) -> AgentOperationAttempt | None:
        """Record a claim: the next attempt, its fence and lease, a running order.

        The core decides whether the order is claimable at all (``Claimed``): a
        queued order, or a retry whose time has come, and never one whose parent
        asked for a cancel.  ``None`` means it is not, and nothing is written.
        """

        now = aware(now)
        row = self.lifecycle(operation, self._attempt_of(operation), parent, now)
        claimed = transition(
            row, Claimed(row.attempt + 1, fence, deadline), self, now
        ).row
        if claimed.state is not State.RUNNING:
            return None
        operation.current_attempt += 1
        operation.state = "running"
        operation.next_action_at = None
        operation.observe_count = 0
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.retry_due_at = None
        # A live attempt has no interrupted reason: keeping the previous one
        # would describe work that is running again.
        operation.status_reason = None
        operation.updated_at = now
        attempt = AgentOperationAttempt(
            operation_id=operation.id,
            attempt=operation.current_attempt,
            fence=fence,
            lease_deadline=deadline,
            agent_certificate_serial=certificate_serial,
            state="running",
            progress=None if progress is None else progress_document(progress),
        )
        session = self._session
        assert session is not None
        session.add(attempt)
        return attempt

    def _attempt_of(self, operation: StoredOperation) -> AgentOperationAttempt | None:
        with self._read() as session:
            return self.attempt_of(session, operation)

    def _parent_of(self, operation: StoredOperation) -> Job | None:
        with self._read() as session:
            return session.get(Job, operation.parent_job_id)

    def reject_attempt(
        self,
        operation: StoredOperation,
        certificate_serial: str,
        fence: str,
        now: datetime,
        *,
        result: AgentFailureResult,
    ) -> AgentOperationAttempt:
        """Record a claim refused before it ran: a failed attempt, a failed order."""

        now = aware(now)
        operation.current_attempt += 1
        operation.state = "failed"
        operation.next_action_at = None
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.updated_at = now
        attempt = AgentOperationAttempt(
            operation_id=operation.id,
            attempt=operation.current_attempt,
            fence=fence,
            lease_deadline=now,
            agent_certificate_serial=certificate_serial,
            state="failed",
            result=json.loads(canonical_message(result)),
        )
        session = self._session
        assert session is not None
        session.add(attempt)
        return attempt

    @staticmethod
    def new_order(**fields: Any) -> StoredOperation:
        """Create an order: it is born ``queued`` with no attempt."""

        return StoredOperation(state="queued", **fields)

    def record_outcome(
        self,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        parent: Job | None,
        outcome: Outcome,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> None:
        """An owner's own decision about the order's end (a validated result, a
        deadline, a cancellation): the core records it as a definite report.

        An end the queue already recorded is withdrawn first, so the owner's
        decision, made later from more evidence, is the one that stands.
        """

        if operation.state in {"succeeded", "failed", "cancelled"}:
            self.reopen(operation, now)
        self.settle(
            operation,
            attempt,
            parent,
            Reported(outcome, retryable=False, reason=reason),
            now,
        )

    @staticmethod
    def reopen(operation: StoredOperation, now: datetime) -> None:
        """Withdraw an end the owner finds premature: the order waits again.

        Only an agent upgrade is reopened (its helper acknowledgement is not proof
        that the target runs); the core then decides what happens to it.
        """

        operation.state = aos.OBSERVING
        operation.next_action_at = None
        operation.observe_count = 0
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.retry_due_at = None
        operation.updated_at = aware(now)

    @staticmethod
    def withdraw_retry(operation: StoredOperation) -> None:
        """Revoke an order's schedule: it can no longer be claimed (and so no
        longer retried), whatever else is decided about it."""

        operation.next_action_at = None
        operation.observe_count = 0
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.retry_due_at = None

    @staticmethod
    def expire_attempt(attempt: AgentOperationAttempt) -> None:
        """An attempt that can no longer report is ``expired``; its fence stays."""

        if attempt.state == "running" or aos.attempt_reported_unknown(attempt):
            aos.lapse(attempt)

    @staticmethod
    def record_report(
        attempt: AgentOperationAttempt,
        state: str,
        result: AgentResultPayload,
    ) -> None:
        """Keep the executor's own report on its attempt (a fact, not a decision)."""

        attempt.result = json.loads(canonical_message(result))
        aos.record_wire_state(attempt, state)


def aggregate_parent_state(
    children: Sequence[tuple[str, bool]], *, cancel_requested: bool
) -> str | None:
    """The state of a parent job from its orders, or ``None`` to leave it alone.

    ``children`` are ``(stored state, retry scheduled)`` pairs.  A parent whose
    every unfinished order is an automatic retry is ``queued``: it is progressing,
    not waiting for a person (the order label ``waiting-for-operator`` is legacy
    for a retry).  Otherwise it ends only when every order has, and the worst
    outcome wins: failed, then waiting, then cancelled, then succeeded.
    """

    children = [
        (
            (LifecycleState.BACKOFF.value if scheduled else adopted.state.value)
            if (adopted := adopt_state(LifecycleSubject.AGENT_OPERATION, state))
            is not None
            else state,
            scheduled,
        )
        for state, scheduled in children
    ]
    final = AGGREGATE_FINAL_STATES
    retrying = [
        i
        for i, (state, scheduled) in enumerate(children)
        if state in aos.PARKED and scheduled
    ]
    if (
        retrying
        and all(
            state == "succeeded" or index in retrying
            for index, (state, _) in enumerate(children)
        )
        and not cancel_requested
    ):
        return "queued"
    # A definite sibling failure can end the batch once the other effects are
    # no longer executing. The caller then cancels their pending retries so a
    # failed parent cannot strand unclaimable work. Observations alone never
    # end a batch or turn it into an operator wait.
    if any(state == LifecycleState.FAILED for state, _ in children) and all(
        state in final or state in aos.PARKED for state, _ in children
    ):
        return LifecycleState.FAILED.value
    if not children or any(state not in final for state, _ in children):
        return None
    states = {state for state, _ in children}
    if "failed" in states:
        return "failed"
    if aos.NEEDS_OPERATOR in states:
        return WAITING
    if "cancelled" in states:
        return "cancelled"
    return "succeeded"


def same_order(left: Lifecycle, right: Lifecycle) -> bool:
    """Whether two reads of an order agree on everything a decision depends on.

    The cancel request's *instant* is left out: for a malformed flag it is
    ``now``, which differs between two reads of the same row.
    """

    return (
        left.state,
        left.attempt,
        left.fence,
        left.next_action_at,
        left.cancel_requested,
        left.effect,
    ) == (
        right.state,
        right.attempt,
        right.fence,
        right.next_action_at,
        right.cancel_requested,
        right.effect,
    )


def set_parent_state(
    job: Job,
    state: str,
    reason: str | None,
    now: datetime,
    *,
    keep_reason: bool = False,
) -> None:
    """The only writer of a parent job's state for an order's lifecycle."""

    job.state = state
    if not keep_reason:
        job.status_reason = None if reason is None else reason[:1024]
    job.updated_at = aware(now)


def adopt_legacy_orders(connection: Connection) -> int:
    """Startup adoption: move the legacy retry encoding onto ``next_action_at``.

    Idempotent and bounded to waiting orders that still carry the retry
    disposition of their current attempt.  Everything else is adopted lazily by
    :meth:`AgentOperationAdapter.adopt` on the row's first transition, and a
    waiting order with no schedule is re-evaluated by the reconciler on its first
    pass, so a missed row heals instead of waiting.
    """

    result = connection.execute(
        update(StoredOperation)
        .where(
            StoredOperation.state.in_(aos.PARKED),
            StoredOperation.next_action_at.is_(None),
            StoredOperation.retry_disposition == _LEGACY_RETRY,
            StoredOperation.retry_disposition_attempt
            == StoredOperation.current_attempt,
        )
        .values(
            next_action_at=func.coalesce(
                StoredOperation.retry_due_at, StoredOperation.updated_at
            ),
            retry_disposition=None,
            retry_disposition_attempt=None,
            retry_due_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def lapsed_running_orders(now: datetime):
    """A SQL predicate: ``running`` orders whose current attempt has no live lease.

    An order with a live lease reports its own outcome and is left alone.  (A
    start's open launch budget also keeps an attempt live; that is decided in
    Python by ``attempt_is_live``, which the sweep applies to each candidate.)
    """

    live = (
        select(AgentOperationAttempt.id)
        .where(
            AgentOperationAttempt.operation_id == StoredOperation.id,
            AgentOperationAttempt.attempt == StoredOperation.current_attempt,
            AgentOperationAttempt.state == "running",
            AgentOperationAttempt.lease_deadline > now,
        )
        .exists()
    )
    return and_(StoredOperation.state == "running", ~live)


def parked_orders(now: datetime):
    """A SQL predicate: waiting orders the reconciler must look at now.

    A wait with no schedule (an operator wait, or a legacy wait to re-evaluate), and
    an order being observed whose time to look again has come.  A scheduled retry
    is claimed by the Spark and is not the reconciler's.
    """

    return and_(
        StoredOperation.state.in_(aos.PARKED),
        or_(
            StoredOperation.next_action_at.is_(None),
            and_(
                StoredOperation.observe_count > 0,
                StoredOperation.next_action_at <= now,
            ),
        ),
    )


__all__ = [
    "IRREVERSIBLE_OPERATIONS",
    "JOB_RUN_OPERATION",
    "OBSERVE_BUDGET",
    "OPERATOR_ACTIONS",
    "OWNER_KIND",
    "RESTART_REISSUE_OPERATIONS",
    "STOP_ACTION",
    "AgentOperationAdapter",
    "adopt_legacy_orders",
    "aggregate_parent_state",
    "cancel_requested_at",
    "is_artifact_owned",
    "lapsed_running_orders",
    "parked_orders",
    "retry_scheduled",
    "same_order",
    "set_parent_state",
]
