"""The lifecycle adapter of a profile load (``FleetProfileApplication``).

A profile application is a **composite over Run/Switch**: it has no effect of its
own, its steps are Run/Switch operations (the children, each a composite over
Spark orders, decided by
:class:`~vonk_control.lifecycle.run_switch.RunSwitchAdapter`).  So it never
carries a retry, a lease or a cancel of its own: its state is
``aggregate(children)`` (:func:`~vonk_control.lifecycle.composite.aggregate`), and
this module is the **only** writer of the application's ``state`` and of the state
of its switch-adapter document (the one durable copy of that aggregate the
Controller mirrors onto the row).

* :meth:`FleetProfileAdapter.lifecycle` (the ``adopt`` hook) maps a stored, possibly
  legacy, application onto the core.  A legacy ``waiting-for-operator`` row (the
  mirror of a child that waited for a person) is ``needs-operator`` and is healed
  by :meth:`FleetProfileAdapter.heal`: no profile action exists, so it is
  re-evaluated (it runs again, observing its child), never left waiting;
* :meth:`FleetProfileAdapter.children` reads the Run/Switch children recorded in the
  switch-adapter document (the unissued steps count as queued children), and
  :meth:`FleetProfileAdapter.aggregate_state` is the profile's state from them;
* :meth:`FleetProfileAdapter.settled` feeds an event through the core's
  ``transition`` and writes what it decides with :meth:`FleetProfileAdapter.apply`.

Stored vocabulary is unchanged (the API, the CLI, the web client and the generated
clients read it):

========================  =====================================================
core state                stored application
========================  =====================================================
``queued``                ``queued``
``running``               ``running``
``backoff``/``observing`` ``queued`` while admission retries (``admission_pending``),
                          else ``running`` (a cancel being driven keeps the label)
``needs-operator``        ``waiting-for-operator``: legacy only, read and healed,
                          never written
``succeeded``/``failed``  ``succeeded``/``failed``
``cancelled``             ``cancelled``
========================  =====================================================

A cancel always completes (core rule 4).  The children are stopped and observed;
the cancel ends ``cancelled`` when nothing of them is left, or when the stop budget
(the core's, or the 660 s authority an agent cancellation is already given) is
spent, with the children's effect recorded unknown in the reason and the pending
operation ids kept as the residue.  It never parks the application for a person.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState

from .. import job_states
from ..agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS, aware
from ..fleet_profile_contract import FleetProfileApplicationProgress
from ..models import FleetProfileApplication, Job
from .adapter import Dispatch
from .composite import aggregate
from .core import RECOVERY, STOP_BUDGET, transition
from .reconciler import Settled
from .reconciler import settle as settle_event
from .run_switch import RunSwitchAdapter
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

KIND = "fleet-profile"
WAITING = LifecycleState.NEEDS_OPERATOR.value
SUPERSEDED = LifecycleState.SUPERSEDED.value
KEEP: Any = object()
#: How long a cancel may spend stopping and observing its children before it ends
#: with their effect recorded as unknown: the authority an agent cancellation of a
#: superseded workload is already given (one constant, in ``agent_operation_facts``).
CANCEL_BUDGET = timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS)
_MAX_REASON = 512
_STORED = {
    State.QUEUED: "queued",
    State.RUNNING: "running",
    State.BACKOFF: "running",
    State.OBSERVING: "running",
    State.NEEDS_OPERATOR: WAITING,
    State.SUCCEEDED: "succeeded",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
}
_RESIDUE = (
    "the stop of its children could not be confirmed in time; cancelled with their "
    "effect unknown (their own lifecycle keeps them): "
)

#: Stops what the cancelled application's children still run (inside the caller's
#: session); ``True`` when nothing is left.
Stopper = Callable[[Session, FleetProfileApplication], bool]
#: Read-only: what the children say about the effect (``established`` = nothing left).
Observer = Callable[[Session, FleetProfileApplication], Effect]
#: Called after the application's state was written (the owner releases claims).
AfterState = Callable[[Session, FleetProfileApplication], None]
#: Called when a cancel of the application ended (the owner closes its records);
#: the flag says its stop was never confirmed (the pending operations are residue).
Finish = Callable[[Session, FleetProfileApplication, bool], None]


def doc_state(document: dict[str, object], state: str) -> None:
    """The only writer of the switch-adapter document's ``state``.

    The document is the durable copy of the profile's aggregate; the application row
    mirrors it through :meth:`FleetProfileAdapter.apply`.
    """

    document["state"] = state


def cancellation_state(progress: dict[str, object], state: str) -> None:
    """The only writer of a cancellation's ``state`` (a record of the cancel the
    application's own state follows: ``observing`` until it ends, then ``cancelled``)."""

    cancellation = progress.get("cancellation")
    if isinstance(cancellation, dict):
        cancellation["state"] = state


class FleetProfileAdapter:
    """``KindAdapter`` for a stored profile application."""

    kind = KIND

    def __init__(
        self,
        session: Session | None = None,
        *,
        sessions: sessionmaker[Session] | None = None,
        clock: Callable[[], datetime] | None = None,
        stopper: Stopper | None = None,
        observer: Observer | None = None,
        after_state: AfterState | None = None,
        finish: Finish | None = None,
    ) -> None:
        self._session = session
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stopper = stopper
        self._observer = observer
        self._after_state = after_state
        self._finish = finish

    def bound(self, session: Session) -> FleetProfileAdapter:
        """The same adapter, reading and stopping through ``session``."""

        return FleetProfileAdapter(
            session,
            sessions=self._sessions,
            clock=self._clock,
            stopper=self._stopper,
            observer=self._observer,
            after_state=self._after_state,
            finish=self._finish,
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

    def adopt(self, stored: FleetProfileApplication) -> Lifecycle:
        """Map a stored (possibly legacy) application onto the core."""

        return self.lifecycle(stored, _progress(stored), self.now())

    def lifecycle(
        self,
        application: FleetProfileApplication,
        progress: FleetProfileApplicationProgress | None,
        now: datetime,
    ) -> Lifecycle:
        """The lifecycle row of a stored application, defaulting what a legacy row lacks.

        An application whose progress cannot be read still maps (to its stored
        state, without a clock): a damaged document is the owner's to report, and
        the core never raises for it.
        """

        now = aware(now)
        document = None if progress is None else progress.switch_adapter
        stored = application.state
        cancellation = None if progress is None else progress.cancellation
        due: datetime | None = None
        retry_count = 0
        if stored == "queued":
            if progress is not None and progress.admission_pending:
                state = State.BACKOFF
                due = (
                    None
                    if progress.admission_retry_at is None
                    else aware(progress.admission_retry_at)
                )
                retry_count = progress.admission_attempt
            else:
                state = State.QUEUED
        elif stored == "running":
            state = State.RUNNING
            if document is not None and document.observation_due_at is not None:
                due = aware(document.observation_due_at)
        elif job_states.means(stored, State.NEEDS_OPERATOR):
            state = State.NEEDS_OPERATOR
        elif stored == "succeeded":
            state = State.SUCCEEDED
        elif stored == "failed":
            state = State.FAILED
        elif stored in {"cancelled", SUPERSEDED}:
            # A superseded application is a definite, non-failed end: the core
            # sees a cancelled intent whose work a successor continues.
            state = State.CANCELLED
        else:  # a state this adapter does not know: re-evaluate it
            state = State.NEEDS_OPERATOR
        # A workload fence precedes every workload effect, so a fenced load may
        # have issued something even before its first child exists.
        issued = (
            application.current_operation_id is not None
            or (document is not None and document.active_operation_id is not None)
            or (progress is not None and progress.workload_intent_ordinal is not None)
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
        if cancellation is not None:
            requested_at = aware(cancellation.requested_at)
            request_key = cancellation.request_key
            if cancellation.observation_due_at is not None:
                due = aware(cancellation.observation_due_at)
            attempts = 0 if document is None else document.stop_reissue_attempt
            observe_count = (
                STOP_BUDGET
                if now >= requested_at + CANCEL_BUDGET
                else min(attempts, STOP_BUDGET - 1)
            )
        ordinal = None if progress is None else progress.workload_intent_ordinal
        return Lifecycle(
            id=application.id,
            kind=KIND,
            state=state,
            attempt=1 if issued else 0,
            next_action_at=None if state in TERMINAL_STATES else due,
            retry_count=retry_count,
            observe_count=observe_count,
            intent_ordinal=ordinal,
            cancel_requested_at=requested_at,
            cancel_request_key=request_key,
            effect=effect,
            reason=application.status_reason,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """A composite has no effect of its own: its children own theirs."""

        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return None

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The step queue is driven by the application's own tick, not here."""

        return Dispatch(issued=False)

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """None: a load waits for no person.  (``retry`` is offered on a failed
        load, which is an ended state, not a wait.)"""

        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        """The Run/Switch children of the load: the issued ones and the queued steps."""

        with self._read() as session:
            application = session.get(FleetProfileApplication, row.id)
            if application is None:
                return ()
            return self.children_of(session, application)

    def children_of(
        self, session: Session, application: FleetProfileApplication
    ) -> tuple[Lifecycle, ...]:
        """One lifecycle row per Run/Switch child of the load.

        The children already finished are the receipts recorded in the
        switch-adapter document (their closed state); the one in flight is its
        Run/Switch operation, adopted by :class:`RunSwitchAdapter` (so a legacy
        operation maps the way it always does); each step not yet issued is a
        queued child (its operation does not exist yet).
        """

        progress = _progress(application)
        document = None if progress is None else progress.switch_adapter
        if document is None:
            return ()
        rows: list[Lifecycle] = [
            _recorded(child.operation_id, child.state) for child in document.children
        ]
        remaining = max(len(document.queue) - document.position, 0)
        active = document.active_operation_id
        if active is not None:
            remaining = max(remaining - 1, 0)
            job = session.get(Job, active)
            rows.append(
                _recorded(active, "running")
                if job is None
                else RunSwitchAdapter(session, clock=self._clock).adopt(job)
            )
        rows.extend(
            Lifecycle(id=f"{application.id}:queued:{index}", kind=KIND)
            for index in range(remaining)
        )
        return tuple(rows)

    def aggregate_state(self, children: Sequence[Lifecycle]) -> State | None:
        """The profile's state from its children (``None`` when it has none)."""

        return aggregate(children)

    @staticmethod
    def recorded_aggregate(recorded: Sequence[object]) -> State | None:
        """The aggregate of children recorded in the switch-adapter document.

        Each is a receipt with a closed state (``succeeded``/``failed``/``cancelled``),
        so the aggregate is exact: worst outcome wins.
        """

        rows = [
            _recorded(f"recorded:{index}", str(item.get("state")))
            for index, item in enumerate(recorded)
            if isinstance(item, Mapping)
        ]
        return aggregate(rows)

    def observe(self, row: Lifecycle) -> Observed:
        """Read-only: whether anything of the children is left."""

        if self._observer is None:
            return Observed(Effect.UNKNOWN, "no observer is bound")
        with self._read() as session:
            application = session.get(FleetProfileApplication, row.id)
            if application is None:
                return Observed(Effect.NONE)
            return Observed(self._observer(session, application))

    def stop(self, row: Lifecycle) -> StopResult:
        """Idempotent: stop what the children still run, when it can be stopped."""

        if self._stopper is None:
            return StopResult.UNCONFIRMED
        with self._read() as session:
            application = session.get(FleetProfileApplication, row.id)
            if application is None:
                return StopResult.CONFIRMED
            confirmed = self._stopper(session, application)
        return StopResult.CONFIRMED if confirmed else StopResult.UNCONFIRMED

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        application: FleetProfileApplication,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
        *,
        reason: str | None = KEEP,
        terminal_reason: str | None = KEEP,
        visible: str | None = None,
        supersession: tuple[str, str | None] | None = None,
        session: Session | None = None,
    ) -> None:
        """Project a core decision onto the stored application; the only such writer.

        The row's label follows the decision; a cancel being driven keeps showing
        what the application was doing.  The owner's hook runs after every write
        of the state (it releases the claims no assignment owns).
        """

        now = aware(now)
        state = _STORED[after.state]
        if after.state in {State.OBSERVING, State.BACKOFF}:
            # A cancel being driven shows as running (it reconciles issued effects);
            # any other decision that only schedules keeps the label it had.
            state = (
                "running"
                if after.cancel_requested
                else application.state
                if application.state in {"queued", "running"}
                else state
            )
        if visible is not None and not after.terminal:
            state = visible
        if supersession is not None and after.state is State.CANCELLED:
            # The one writer of ``superseded``: a cancelled intent a successor
            # (or a later intent) took over, with its typed reason.
            code, successor = supersession
            state = SUPERSEDED
            document = dict(application.progress or {})
            document["supersede_code"] = code
            document["superseded_by"] = successor
            application.progress = document
        application.state = state
        progress = _progress(application)
        if after.terminal and terminal_reason is not KEEP:
            reason = terminal_reason
        if reason is not KEEP:
            application.status_reason = None if reason is None else reason[:_MAX_REASON]
        application.updated_at = now
        if (
            after.state in {State.OBSERVING, State.BACKOFF}
            and after.cancel_requested
            and after.next_action_at is not None
        ):
            self._schedule_cancel_observation(application, after.next_action_at)
        hook_session = session or self._session
        if self._after_state is not None and hook_session is not None:
            self._after_state(hook_session, application)
        if (
            after.state is State.CANCELLED
            and self._finish is not None
            and hook_session is not None
            and progress is not None
            and progress.cancellation is not None
        ):
            self._finish(hook_session, application, after.effect is Effect.UNKNOWN)

    @staticmethod
    def _schedule_cancel_observation(
        application: FleetProfileApplication, due: datetime
    ) -> None:
        """Persist when the cancel is looked at next: the later of the core's
        backoff and an observation an owner already scheduled (an effect's own)."""

        document: Any = application.progress
        raw: Any = document.get("cancellation") if isinstance(document, dict) else None
        if not isinstance(raw, dict):
            return
        cancellation: dict[str, Any] = dict(raw)
        stored = cancellation.get("observation_due_at")
        try:
            existing = (
                aware(datetime.fromisoformat(stored))
                if isinstance(stored, str)
                else None
            )
        except ValueError:
            existing = None
        chosen = due if existing is None or existing <= due else existing
        cancellation["observation_due_at"] = aware(chosen).isoformat()
        application.progress = {**document, "cancellation": cancellation}

    # ---------------------------------------------------------------- settle

    def settled(
        self,
        application: FleetProfileApplication,
        event: Event,
        now: datetime,
        *,
        session: Session | None = None,
        unflag: bool = False,
        **write: Any,
    ) -> Settled:
        """Feed ``event`` to the core and write what it decides, inline.

        The caller holds the application's lock.  An event the application absorbs
        (it already ended) writes nothing.
        """

        now = aware(now)
        progress = _progress(application)
        row = self.lifecycle(application, progress, now)
        if unflag:
            # The owner recorded the request before asking; the core sets its own
            # monotonic flag from the event.
            row = replace(row, cancel_requested_at=None, cancel_request_key=None)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            self.apply(application, before, after, now, session=session, **write)
            return True

        pending = (
            ()
            if progress is None or progress.cancellation is None
            else tuple(progress.cancellation.pending_operation_ids)
        )

        def residue(_row: Lifecycle, _reason: str) -> None:
            # The cancel gave up confirming the stop: the residue record is the
            # reason (and the pending operation ids stay in the cancellation).
            application.status_reason = self.residue_reason(
                "Profile application cancelled", pending
            )

        bound = self if session is None else self.bound(session)
        return settle_event(row, event, bound, now, save=save, residue=residue)

    def decide(
        self,
        application: FleetProfileApplication,
        event: Event,
        now: datetime,
    ) -> Decision:
        """The core's decision for ``event`` without writing it (tests and probes)."""

        now = aware(now)
        row = self.lifecycle(application, _progress(application), now)
        return transition(row, event, self, now)

    # ------------------------------------------- the application's own events

    @staticmethod
    def new_application(*, state: str, **fields: Any) -> FleetProfileApplication:
        """A new application: ``queued`` (or ``succeeded`` when there is nothing to do)."""

        return FleetProfileApplication(state=state, **fields)

    def reset(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        steps: bool,
        session: Session | None = None,
    ) -> None:
        """A retained receipt starts a new attempt: ``queued``, or ``succeeded`` if
        there is nothing to do.  Not a transition of the old attempt: its row is
        rewritten for the new one."""

        before = self.lifecycle(application, _progress(application), now)
        after = replace(before, state=State.QUEUED if steps else State.SUCCEEDED)
        self.apply(application, before, after, now, reason=None, session=session)

    def reopen(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        reason: str,
        session: Session | None = None,
    ) -> Lifecycle:
        """An ended load whose child is still live runs again (the retry owner's
        explicit decision, the way an order is reopened before the core decides it).

        Not a transition of the ended attempt: the same authorized work continues,
        and the retry schedule rate-limits it.
        """

        now = aware(now)
        before = self.lifecycle(application, _progress(application), now)
        after = replace(before, state=State.RUNNING, effect=Effect.ISSUED)
        self.apply(application, before, after, now, reason=reason, session=session)
        return after

    def project(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        state: State,
        reason: str | None = KEEP,
        session: Session | None = None,
    ) -> Lifecycle:
        """Show what the load is doing (a projection of its children, not a decision)."""

        now = aware(now)
        before = self.lifecycle(application, _progress(application), now)
        if before.terminal:
            return before
        after = replace(before, state=state)
        self.apply(application, before, after, now, reason=reason, session=session)
        return after

    def succeed(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        reason: str | None = None,
        session: Session | None = None,
    ) -> Lifecycle:
        return self.settled(
            application, Reported(Outcome.DONE), now, session=session, reason=reason
        ).row

    def fail(
        self,
        application: FleetProfileApplication,
        reason: str,
        now: datetime,
        *,
        session: Session | None = None,
    ) -> Lifecycle:
        """A definite end of the load: its steps ended, or its stored contract cannot
        be read (the document is the evidence of what was issued: retained)."""

        return self.settled(
            application,
            Reported(Outcome.FAILED, retryable=False, reason=reason),
            now,
            session=session,
            reason=reason,
        ).row

    def cancelled(
        self,
        application: FleetProfileApplication,
        reason: str,
        now: datetime,
        *,
        effect: Effect = Effect.STOPPED,
        session: Session | None = None,
    ) -> Lifecycle:
        """A definite cancel: superseded by a newer intent, or the children stopped."""

        return self.settled(
            application,
            Reported(Outcome.CANCELLED, effect=effect, reason=reason),
            now,
            session=session,
            reason=reason,
        ).row

    @staticmethod
    def retry_pending(application: FleetProfileApplication) -> bool:
        """An application that ended (or waits) while a retry is still scheduled."""

        return application.state == State.FAILED.value or job_states.means(
            application.state, State.NEEDS_OPERATOR
        )

    def supersede(
        self,
        application: FleetProfileApplication,
        reason: str,
        now: datetime,
        *,
        code: str,
        by: str | None = None,
        effect: Effect = Effect.STOPPED,
        session: Session | None = None,
    ) -> Lifecycle:
        """A definite end because a successor (``by``) or a later intent took the
        work over.  Terminal ``superseded``, never ``failed``: the client follows
        ``superseded_by`` instead of reporting a fault."""

        now = aware(now)
        before = self.lifecycle(application, _progress(application), now)
        if before.state is State.FAILED:
            # The core absorbs events on an ended row; a failed one that a retry
            # was still scheduled for is relabelled (nothing is issued or stopped).
            document = dict(application.progress or {})
            document["supersede_code"] = code
            document["superseded_by"] = by
            document["retry_due_at"] = None
            document["blockers"] = []
            application.progress = document
            application.state = SUPERSEDED
            application.status_reason = reason[:_MAX_REASON]
            application.updated_at = now
            hook_session = session or self._session
            if self._after_state is not None and hook_session is not None:
                self._after_state(hook_session, application)
            return replace(before, state=State.CANCELLED, reason=reason)
        return self.settled(
            application,
            Reported(Outcome.CANCELLED, effect=effect, reason=reason),
            now,
            session=session,
            reason=reason,
            supersession=(code, by),
        ).row

    def request_cancel(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        reason: str,
        session: Session | None = None,
        run_commands: bool = True,
    ) -> Lifecycle:
        """Rule 4: a cancel completes.  Nothing issued ends at once; otherwise the
        children are stopped and observed, and the budget bounds the wait.

        ``run_commands=False`` records the decision without running the stop now (an
        owner inside a transaction it must not extend): the worker's next tick does.
        """

        now = aware(now)
        progress = _progress(application)
        cancellation = None if progress is None else progress.cancellation
        key = None if cancellation is None else cancellation.request_key
        event = CancelRequested(key, reason)
        if run_commands:
            return self.settled(
                application,
                event,
                now,
                session=session,
                unflag=True,
                reason=reason,
            ).row
        before = replace(
            self.lifecycle(application, progress, now),
            cancel_requested_at=None,
            cancel_request_key=None,
        )
        decision = transition(before, event, self, now)
        row = decision.row
        if not row.terminal:
            # The first stop belongs to the worker's next pass, which is due now
            # (no stored clock: an instant spelled two ways compares as later).
            row = replace(row, next_action_at=None)
        self.apply(application, before, row, now, reason=reason, session=session)
        return row

    def tick_cancel(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        terminal_reason: str,
        session: Session | None = None,
    ) -> Settled:
        """One stop attempt of a cancel in flight (the core spaces and bounds them)."""

        return self.settled(
            application,
            Tick(),
            now,
            session=session,
            terminal_reason=terminal_reason,
        )

    @staticmethod
    def residue_reason(base: str, pending: Sequence[str]) -> str:
        """The reason a cancel ended with its stop unconfirmed (the residue record)."""

        names = ", ".join(sorted(pending)[:8])
        text = f"{base}; {_RESIDUE}{names}" if names else f"{base}; {_RESIDUE}".rstrip()
        return text[:_MAX_REASON]

    @staticmethod
    def next_retry(operation_id: str, count: int, now: datetime) -> datetime:
        """The one retry clock (the core's stable bounded backoff), for the retries a
        load keeps in its progress: parked recovery and admission."""

        scheduled = RECOVERY.next_attempt(
            operation_id, max(count, 1), aware(now), ongoing_intent=True
        )
        assert scheduled is not None  # ongoing intent never exhausts
        return scheduled

    def heal_superseded(
        self,
        application: FleetProfileApplication,
        now: datetime,
    ) -> bool:
        """Re-label a legacy ended row that was really a supersession.

        An older Controller recorded a replaced application as ``failed`` (the
        parent of an automatic retry) or ``cancelled`` (a replaced order); clients
        then reported a fault.  Only the label and typed reason change: the row was
        already ended and no effect is touched.
        """

        found = legacy_supersession(application.state, application.status_reason)
        if found is None:
            return False
        code, successor = found
        document = dict(application.progress or {})
        document["supersede_code"] = code
        document["superseded_by"] = successor
        application.progress = document
        application.state = SUPERSEDED
        application.updated_at = aware(now)
        return True

    def heal(
        self,
        application: FleetProfileApplication,
        now: datetime,
        *,
        session: Session | None = None,
    ) -> Lifecycle:
        """Re-evaluate a legacy ``waiting-for-operator`` load (rules 1 and 3).

        No profile action exists and nothing here is irreversible, so it is not
        left waiting for a person: it runs again (its child is observed) or, when
        it was still being admitted, retries its admission.
        """

        now = aware(now)
        progress = _progress(application)
        row = self.lifecycle(application, progress, now)
        if row.state is not State.NEEDS_OPERATOR:
            return row
        admitting = progress is not None and progress.admission_pending
        after = replace(row, state=State.BACKOFF if admitting else State.RUNNING)
        self.apply(
            application,
            row,
            after,
            now,
            visible="queued" if admitting else "running",
            session=session,
        )
        return after


#: What an older Controller wrote when a successor or later intent replaced an
#: application: ``failed`` for an automatic retry's parent, ``cancelled`` for a
#: replaced order.  ``legacy_supersession`` reads it back as the typed reason.
RETRY_REASON_PREFIX = "Automatically reconciled by profile retry "
_LEGACY_INTENT_PREFIXES = (
    "Profile order was replaced",
    "Pending profile intent was superseded by",
    "Pending profile workload intent was superseded",
    "Profile intent was superseded by",
    "Profile load was superseded by",
    "Profile workload intent was superseded",
    "A later accepted profile intent owns",
)
LEGACY_SUPERSEDED_PREFIXES = (
    RETRY_REASON_PREFIX,
    "Pending profile intent was superseded: ",
    *_LEGACY_INTENT_PREFIXES,
)


def legacy_supersession(
    stored: str, reason: str | None
) -> tuple[str, str | None] | None:
    """``(code, successor)`` of a legacy ended row that was really a supersession."""

    text = (reason or "").strip()
    if stored == State.FAILED.value and text.startswith(RETRY_REASON_PREFIX):
        successor = text[len(RETRY_REASON_PREFIX) :].split(";", 1)[0].strip()
        try:
            uuid.UUID(successor)
        except ValueError:
            return None
        return ("superseded-by-retry", successor)
    if stored == State.CANCELLED.value:
        if text.startswith("Pending profile intent was superseded: "):
            return ("effects-changed-during-admission", None)
        if text.startswith(_LEGACY_INTENT_PREFIXES):
            return ("superseded-by-intent", None)
    return None


def _recorded(identity: str, stored: str) -> Lifecycle:
    """A child that finished (or whose state was recorded): its stored state."""

    # A state it does not know is observed, never parked.
    state = job_states.core(stored) or State.OBSERVING
    effect = {
        State.SUCCEEDED: Effect.ESTABLISHED,
        State.CANCELLED: Effect.STOPPED,
    }.get(state, Effect.UNKNOWN)
    return Lifecycle(id=identity, kind=KIND, state=state, attempt=1, effect=effect)


def _progress(
    application: FleetProfileApplication,
) -> FleetProfileApplicationProgress | None:
    """The typed progress, or ``None`` when the stored document is damaged."""

    try:
        return FleetProfileApplicationProgress.model_validate_json(
            _canonical(application.progress)
        )
    except (ValueError, TypeError):
        return None


def _canonical(value: object) -> str:
    return json.dumps(value, default=str)


__all__ = [
    "CANCEL_BUDGET",
    "KEEP",
    "KIND",
    "WAITING",
    "FleetProfileAdapter",
    "cancellation_state",
    "doc_state",
]
