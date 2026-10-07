"""The lifecycle adapter of the NAS model cache (``ModelCacheOperation``).

``model_cache`` keeps what is specific to the cache: SQL, the transfer pool, the
object locks, the per-object receipts, the removal fences and checkpoints.  This
module owns the rest: what a stored (possibly legacy) operation *is* to the core
(:meth:`ModelCacheAdapter.lifecycle`, the ``adopt`` hook), what the kind knows
that the core asks (nothing here is irreversible, the retry fence, a stop), and
the **only** projection of a core decision back onto the stored row
(:meth:`ModelCacheAdapter.apply`).  No other code writes an operation's ``state``.

Stored encoding.  The stored ``state`` is a word of the core vocabulary
(``vonk_agent_protocol.LifecycleState``); a row written before the rename may still
say ``partial``, which the contract adopts as ``backoff`` (readers select by
``model_cache_states.LIVE`` and friends, never by a hand-spelled word).  The core's
states are projected onto it:

=====================  ========================================================
core state             stored operation
=====================  ========================================================
``queued``             ``queued``, ``next_action_at`` NULL (claimable now)
``backoff``            ``queued`` (a failure's retry, a busy writer) or
                       ``backoff`` (interrupted, removal deferral) with
                       ``next_action_at`` set (the one retry clock)
``running``            ``running`` with ``fence`` (the claiming process) and
                       ``lease_deadline`` (replaces the payload ``claim``)
``observing``          the current stored state with ``observe_count > 0``: only
                       a cancel observes (a stop and its confirmation)
``needs-operator``     never built for this kind (see below); a legacy row that
                       maps here is re-evaluated and retried on its first tick
``succeeded`` etc.     ``succeeded`` / ``failed`` / ``cancelled``
=====================  ========================================================

Nothing is irreversible: a download or repair resumes its exact transfer ledger
(content-addressed objects, each published once under its lock), a removal
resumes its fenced checkpoint (``removal_fence`` plus ``object_index`` and
``set_index``; repeating a step is a no-op).  So rule 1 retries every uncertain
outcome and the core never parks one of these for an operator: there is no
``waiting-for-operator`` here and no operator action to advertise.  A failure the
cache itself calls terminal (a credential, an untrusted source) is a definite
``failed``; only a changed credential or a new request revives it, through
:meth:`ModelCacheAdapter.reopen`, which withdraws the end and lets the core
decide again.

A removal has no lease: its steps are short, fenced transactions the worker
advances one per tick, so a stored ``running`` removal is a queued one between
two steps (core ``queued``) and a lapsed process costs nothing.

Legacy rows: ``payload.retry.next_retry_at`` / ``retry_after_seconds`` and
``payload.claim`` are read by ``adopt`` when the columns are empty, never
written; ``apply`` strips them on the row's next transition and
:func:`adopt_legacy_operations` moves them at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from .. import model_cache_states
from ..agent_operation_facts import aware
from ..models import ModelCacheOperation
from .adapter import Dispatch
from .agent_operation import same_order
from .core import transition
from .evidence import Damaged
from .reconciler import settle
from .types import (
    TERMINAL_STATES,
    Claimed,
    Decision,
    Effect,
    Event,
    Heartbeat,
    LeaseLapsed,
    Lifecycle,
    Observed,
    Outcome,
    Reported,
    State,
    StopResult,
)

_LOGGER = logging.getLogger(__name__)
#: States an operation is active in (everything that is not an end).
ACTIVE_STORED_STATES = model_cache_states.LIVE
_ENDED = frozenset({"succeeded", "failed", "cancelled"})
_MAX_REASON = 512
#: How many active operations one sweep looks at.  Active cache operations number
#: in the tens; the bound only keeps a runaway table from stalling the worker.
SWEEP_LIMIT = 500


class CacheEffects(Protocol):
    """What the cache itself knows that the core's questions need (read-only)."""

    def running_here(self) -> frozenset[str]:
        """Operations this process is transferring right now."""
        ...

    def objects_present(self, payload: Mapping[str, object]) -> bool | None:
        """Whether every object of the operation's set has a receipt in storage,
        or ``None`` when storage could not be read."""
        ...

    def set_is_cached(self, set_digest: str | None) -> bool:
        """Whether the set's row already records the published, cached set."""
        ...

    def effects_settled(self, operation_id: str, payload: Mapping[str, object]) -> bool:
        """Signal the operation's transfers to stop; whether no writer of one of
        its objects is still active (non-blocking)."""
        ...

    def cooldown_until(self, payload: Mapping[str, object]) -> datetime | None:
        """A provider-wide cooldown the operation's sources are under."""
        ...


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def legacy_retry_due(payload: Mapping[str, object]) -> datetime | None:
    """The retry instant of a row written before the core, else ``None``."""

    retry = payload.get("retry")
    return _parse(retry.get("next_retry_at")) if isinstance(retry, Mapping) else None


def legacy_claim(payload: Mapping[str, object]) -> tuple[str, datetime] | None:
    """The claim (owner, expiry) of a row written before the core, else ``None``."""

    claim = payload.get("claim")
    if not isinstance(claim, Mapping):
        return None
    owner, expires = claim.get("owner"), _parse(claim.get("expires_at"))
    if isinstance(owner, str) and owner and expires is not None:
        return owner, expires
    return None


def cancellation_of(
    payload: Mapping[str, object], now: datetime
) -> tuple[datetime | None, str | None]:
    """The monotonic cancel request recorded in the payload: instant and key."""

    raw = payload.get("cancellation")
    if not isinstance(raw, Mapping):
        return None, None
    requested = _parse(raw.get("requested_at")) or now
    key = raw.get("request_key")
    return aware(requested), key if isinstance(key, str) else None


class ModelCacheAdapter:
    """``KindAdapter`` for a stored cache operation.

    ``scope`` opens a session for a read (the service's own session scope, so a
    caller-bound session is reused and a factory yields short sessions).
    ``read_payload``/``store_payload`` are the service's validated payload codec;
    they are injected so this module does not import the service it serves.
    ``on_end`` runs inside the transaction that ends an operation (the set
    projection of a cancelled download).
    """

    kind = "model-cache"

    def __init__(
        self,
        scope: Callable[..., AbstractContextManager[Session]],
        *,
        clock: Callable[[], datetime] | None = None,
        effects: CacheEffects,
        read_payload: Callable[[ModelCacheOperation], dict[str, object] | Damaged],
        store_payload: Callable[[ModelCacheOperation, Mapping[str, object]], None],
        on_end: Callable[[ModelCacheOperation, Lifecycle, Lifecycle], None]
        | None = None,
    ) -> None:
        self._scope = scope
        self._clock = clock or (lambda: datetime.now(UTC))
        self._effects = effects
        self._read_payload = read_payload
        self._store_payload = store_payload
        self._on_end = on_end

    # ------------------------------------------------------------- plumbing

    def now(self) -> datetime:
        return aware(self._clock())

    def _payload(self, operation: ModelCacheOperation) -> Mapping[str, object]:
        """The operation's payload; a corrupt one is read as far as it goes.

        Bookkeeping never raises here (rule 5): an unreadable envelope only means
        the row has no cancel request and no legacy retry or claim to adopt.
        """

        try:
            payload = self._read_payload(operation)
        except Exception:  # noqa: BLE001 - corrupt bookkeeping is unknown, not fatal
            payload = Damaged("the operation envelope does not read")
        if isinstance(payload, Damaged):
            raw = operation.payload
            return raw if isinstance(raw, Mapping) else {}
        return payload

    # ---------------------------------------------------------------- adopt

    def adopt(self, stored: ModelCacheOperation) -> Lifecycle:
        return self.lifecycle(stored, self.now())

    def lifecycle(self, operation: ModelCacheOperation, now: datetime) -> Lifecycle:
        """The lifecycle row of a stored operation, defaulting what a legacy row lacks."""

        now = aware(now)
        payload = self._payload(operation)
        retry = payload.get("retry")
        attempts = (
            retry.get("automatic_attempts") if isinstance(retry, Mapping) else None
        )
        if type(attempts) is not int or attempts < 1:
            attempts = max(int(operation.attempt or 1), 1)
        requested_at, request_key = cancellation_of(payload, now)
        fence = operation.fence
        lease = (
            None
            if operation.lease_deadline is None
            else aware(operation.lease_deadline)
        )
        claim = legacy_claim(payload) if lease is None else None
        if claim is not None:
            fence, lease = claim[0], aware(claim[1])
        next_action = (
            aware(operation.next_action_at)
            if operation.next_action_at is not None
            else legacy_retry_due(payload)
        )
        observe = operation.observe_count or 0
        stored = operation.state
        removal = operation.kind == "remove"
        live_here = operation.id in self._effects.running_here()
        if stored == "succeeded":
            state = State.SUCCEEDED
        elif stored == "failed":
            state = State.FAILED
        elif stored == "cancelled":
            state = State.CANCELLED
        elif removal:
            # A removal has no lease: its stored ``running`` is "between steps".
            lease = None
            state = State.BACKOFF if next_action is not None else State.QUEUED
        elif observe > 0:
            state = State.OBSERVING
            next_action = next_action or now
            lease = None
        elif live_here and stored == "running":
            state = State.RUNNING
            lease = max(lease or now, now + timedelta(seconds=1))
            next_action = lease
        elif lease is not None and (stored == "running" or claim is not None):
            state, next_action = State.RUNNING, lease
        elif stored == "running":
            # A running row with no claim at all: nobody can be running it.
            state = State.RUNNING
            lease = next_action = aware(operation.updated_at)
        elif next_action is not None:
            state = State.BACKOFF
        elif stored == "queued":
            state = State.QUEUED
        else:
            state = State.BACKOFF  # an interrupted (partial) row with no schedule
        if state is State.SUCCEEDED:
            effect = Effect.ESTABLISHED
        elif state is State.CANCELLED:
            # The stored ending fences the intent, not the physical writer.
            # Cancellation can expire with an unconfirmed stop. This row has
            # no persisted stop observation, so its state cannot prove absence.
            # Exact writer locks and managed receipts still govern fresh work.
            effect = Effect.UNKNOWN
        elif state is State.RUNNING:
            effect = Effect.ISSUED
        else:
            effect = Effect.UNKNOWN
        return Lifecycle(
            id=operation.id,
            kind=operation.kind,
            state=state,
            attempt=max(int(operation.attempt or 1), 1),
            fence=fence,
            lease_deadline=lease if state is State.RUNNING else None,
            next_action_at=next_action,
            retry_count=attempts - 1,
            observe_count=observe,
            cancel_requested_at=requested_at,
            cancel_request_key=request_key,
            effect=effect,
            reason=operation.last_error,
        )

    # ----------------------------------------------------- the kind's facts

    def irreversible(self, row: Lifecycle) -> bool:
        """Nothing here is: every step resumes an exact, content-addressed ledger."""

        return False

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        """A provider cooldown the operation's sources are under."""

        with self._scope() as session:
            operation = session.get(ModelCacheOperation, row.id)
            if operation is None:
                return None
            cooldown = self._effects.cooldown_until(self._payload(operation))
        return None if cooldown is None else aware(cooldown)

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        """The transfer pool claims its own work; a due retry is claimable as it is."""

        return Dispatch(issued=False)

    def observe(self, row: Lifecycle) -> Observed:
        """Read-only: do the object receipts already prove the effect?

        ``established`` only when every object of the set has a receipt *and* the
        set is already recorded as cached; a set whose bytes are all there but is
        not published yet is the transfer's to finish (its next attempt finds the
        bytes and publishes), so it is ``none`` and retried, never marked done by
        an observer.  A removal's effect is observed by its own checkpoint.
        """

        with self._scope() as session:
            operation = session.get(ModelCacheOperation, row.id)
            if operation is None:
                return Observed(Effect.NONE, "the operation no longer exists")
            if operation.kind == "remove":
                return Observed(Effect.UNKNOWN, "a removal resumes its checkpoint")
            payload = self._payload(operation)
            set_digest = operation.artifact_set_sha256
        present = self._effects.objects_present(payload)
        if present is None:
            return Observed(Effect.UNKNOWN, "storage receipts could not be read")
        if present and self._effects.set_is_cached(set_digest):
            return Observed(Effect.ESTABLISHED, "every object has a receipt")
        return Observed(Effect.NONE, "the set is not complete in storage")

    def stop(self, row: Lifecycle) -> StopResult:
        """Idempotent: signal the transfer, confirm once no writer is active.

        Unconfirmed while a transfer of one of the operation's objects still holds
        its lock; the core re-issues it at a bounded rate and, after its budget,
        ends the cancel with the effect unknown (the bytes are content-addressed
        and the durable cancellation fences every later write).
        """

        with self._scope() as session:
            operation = session.get(ModelCacheOperation, row.id)
            if operation is None:
                return StopResult.CONFIRMED
            payload = self._payload(operation)
        try:
            settled = self._effects.effects_settled(row.id, payload)
        except Exception:  # noqa: BLE001 - an unreadable lock is unconfirmed
            settled = False
        return StopResult.CONFIRMED if settled else StopResult.UNCONFIRMED

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        """None: the kind is idempotent, so the core never waits for a person."""

        return ()

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        operation: ModelCacheOperation,
        before: Lifecycle,
        after: Lifecycle,
        now: datetime,
        *,
        interrupted: bool | None = None,
        consume_retry: bool = True,
    ) -> bool:
        """Project a core decision onto the stored operation; the only such writer.

        ``interrupted`` labels a scheduled retry ``partial`` (work that stopped
        part way) rather than ``queued``; by default a retry of a row that was
        running or is already partial is ``partial``.  ``consume_retry=False``
        records a dependency wait (a busy writer) without counting an attempt.
        Returns whether anything changed.
        """

        now = aware(now)
        changed = False

        def put(name: str, value: Any) -> None:
            nonlocal changed
            current = getattr(operation, name)
            if isinstance(current, datetime):
                current = aware(current)
            if current != value:
                setattr(operation, name, value)
                changed = True

        label_partial = (
            interrupted
            if interrupted is not None
            else (
                model_cache_states.operation_is_backoff(operation.state)
                or before.state is State.RUNNING
            )
        )
        state: str
        next_action: datetime | None = None
        lease: datetime | None = None
        match after.state:
            case State.QUEUED:
                state = (
                    model_cache_states.BACKOFF
                    if model_cache_states.operation_is_backoff(operation.state)
                    else "queued"
                )
            case State.BACKOFF:
                state = model_cache_states.BACKOFF if label_partial else "queued"
                next_action = after.next_action_at
            case State.OBSERVING:
                state = (
                    operation.state
                    if operation.state in ACTIVE_STORED_STATES
                    else model_cache_states.BACKOFF
                )
                next_action = after.next_action_at
            case State.RUNNING:
                state = "running"
                lease = after.lease_deadline
            case State.NEEDS_OPERATOR:
                state = model_cache_states.BACKOFF
            case State.SUCCEEDED:
                state = "succeeded"
            case State.FAILED:
                state = "failed"
            case _:
                state = "cancelled"
        if after.state is State.RUNNING and before.state is not State.RUNNING:
            # The attempt number counts consumed attempts (a failure's retry), not
            # claims: waiting for a busy writer is a dependency wait and consumes
            # none, so the same attempt resumes.
            put("attempt", max(int(operation.attempt), after.retry_count + 1))
        put("state", state)
        put("next_action_at", next_action)
        put("lease_deadline", lease)
        put(
            "observe_count",
            after.observe_count if after.state is State.OBSERVING else 0,
        )
        if after.fence is not None:
            put("fence", after.fence)
        ended = after.state in TERMINAL_STATES
        if ended:
            if operation.completed_at is None:
                operation.completed_at = now
                changed = True
        elif operation.completed_at is not None:
            operation.completed_at = None
            changed = True
        changed |= self._tidy_payload(
            operation, after, consume_retry=consume_retry and not ended
        )
        if changed:
            operation.updated_at = now
        if ended and before.state not in TERMINAL_STATES and self._on_end is not None:
            self._on_end(operation, before, after)
        return changed

    def _tidy_payload(
        self, operation: ModelCacheOperation, after: Lifecycle, *, consume_retry: bool
    ) -> bool:
        """Retire the legacy claim and retry clock; count a consumed attempt."""

        try:
            read = self._read_payload(operation)
        except Exception:  # noqa: BLE001 - leave a corrupt envelope for inspection
            return False
        if isinstance(read, Damaged):
            return False  # a corrupt envelope is left for inspection
        payload = dict(read)
        updated = dict(payload)
        retry = payload.get("retry")
        retry_document = dict(retry) if isinstance(retry, Mapping) else None
        if retry_document is not None:
            retry_document["next_retry_at"] = None
            retry_document["retry_after_seconds"] = None
            if after.state is State.BACKOFF and consume_retry:
                retry_document["automatic_attempts"] = after.retry_count + 1
            updated["retry"] = retry_document
        updated.pop("claim", None)
        if updated == payload:
            return False
        self._store_payload(operation, updated)
        return True

    # ---------------------------------------------------------------- settle

    def settle(
        self,
        operation: ModelCacheOperation,
        event: Event,
        now: datetime | None = None,
        *,
        interrupted: bool | None = None,
        consume_retry: bool = True,
    ) -> Lifecycle:
        """Feed ``event`` to the core and write what it decides, inline.

        The caller holds the row's lock; ``operation`` is changed in place.
        """

        now = aware(now or self.now())
        row = self.lifecycle(operation, now)

        def save(before: Lifecycle, after: Lifecycle) -> bool:
            self.apply(
                operation,
                before,
                after,
                now,
                interrupted=interrupted,
                consume_retry=consume_retry,
            )
            return True

        def residue(row: Lifecycle, reason: str) -> None:
            operation.last_error = reason[:_MAX_REASON]

        return settle(row, event, self, now, save=save, residue=residue).row

    def decide(
        self, operation: ModelCacheOperation, event: Event, now: datetime
    ) -> Decision:
        """The core's decision for ``event`` without writing it (tests, probes)."""

        row = self.lifecycle(operation, aware(now))
        return transition(row, event, self, aware(now))

    # ------------------------------------------------------- owner's events

    def claim(
        self,
        operation: ModelCacheOperation,
        owner: str,
        lease_seconds: float,
        now: datetime,
        *,
        ignore_backoff: bool = False,
    ) -> bool:
        """Record a claim: the core decides whether the operation is claimable.

        A queued operation, or a retry whose time has come (``ignore_backoff`` is
        the maintenance path that claims regardless of the retry clock), and never
        one with a cancel request.  Returns whether this owner now holds it.
        """

        now = aware(now)
        row = self.lifecycle(operation, now)
        decide_at = (
            max(now, row.next_action_at)
            if ignore_backoff and row.next_action_at is not None
            else now
        )
        lease = decide_at + timedelta(seconds=lease_seconds)
        after = self.settle(
            operation, Claimed(row.attempt + 1, owner, lease), decide_at
        )
        return after.state is State.RUNNING and after.fence == owner

    def lapse(self, operation: ModelCacheOperation, now: datetime) -> Lifecycle:
        """The running attempt can no longer report: the core retries it."""

        return self.settle(operation, LeaseLapsed("the transfer lease lapsed"), now)

    def renew(
        self,
        operation: ModelCacheOperation,
        owner: str,
        lease_seconds: float,
        now: datetime,
        *,
        take: bool = True,
    ) -> bool:
        """The owner's progress: renew its lease, or take the operation.

        A download that is not ``running`` under this owner is claimed (the
        maintenance path starts a transfer it claimed a moment ago); one running
        under another fence is not this worker's any more (rule 7) and nothing is
        written.  A removal's step only notes that it is under way.  Returns
        whether the owner may carry on.  ``take=False`` is a bare heartbeat: it
        never claims an operation the core has since put back to wait.
        """

        now = aware(now)
        if operation.state in _ENDED:
            return False
        if operation.kind == "remove":
            changed = False
            if operation.state != "running":
                operation.state = "running"
                changed = True
            if operation.next_action_at is not None:
                operation.next_action_at = None
                changed = True
            if operation.last_error is not None:
                operation.last_error = None
                changed = True
            if changed:
                operation.updated_at = now
            return True
        row = self.lifecycle(operation, now)
        if row.cancel_requested:
            return False
        lease = now + timedelta(seconds=lease_seconds)
        if row.state is State.RUNNING:
            if row.fence not in (None, owner):
                return False
            after = self.settle(operation, Heartbeat(owner, lease), now)
            return after.state is State.RUNNING
        if not take:
            return False
        return self.claim(operation, owner, lease_seconds, now, ignore_backoff=True)

    def complete(
        self,
        operation: ModelCacheOperation,
        event: Event,
        now: datetime | None = None,
        *,
        interrupted: bool | None = None,
        consume_retry: bool = True,
    ) -> Lifecycle:
        """An owner's report (success, failure, interruption), through the core.

        A report about an operation that already ended is withdrawn first when
        the owner finds the end premature (see :meth:`reopen`); otherwise the
        core ignores it (an end absorbs events).
        """

        return self.settle(
            operation, event, now, interrupted=interrupted, consume_retry=consume_retry
        )

    @staticmethod
    def reopen(operation: ModelCacheOperation, now: datetime) -> None:
        """Withdraw an end the owner finds premature: the operation is interrupted.

        A changed credential or a recheck that finds the failure transient revives
        a terminal failure; the core then decides what happens to it.
        """

        operation.state = model_cache_states.BACKOFF
        operation.next_action_at = None
        operation.lease_deadline = None
        operation.observe_count = 0
        operation.completed_at = None
        operation.updated_at = aware(now)

    @staticmethod
    def new_operation(**fields: Any) -> ModelCacheOperation:
        """Create an operation: it is born ``queued`` and claimable.

        ``next_action_at`` may be passed for an operation that must wait (a
        download that cannot fit yet): the claim loop leaves it until it is due.
        """

        return ModelCacheOperation(state="queued", **fields)

    def fail_corrupt(
        self, operation: ModelCacheOperation, reason: str, now: datetime
    ) -> None:
        """End an operation whose own document cannot be read (kept for inspection).

        A definite failure (rule 5 covers bookkeeping that *can* be reconciled; a
        document that cannot be read cannot be resumed): the core records the end.
        """

        operation.last_error = reason[:_MAX_REASON]
        self.settle(
            operation, Reported(Outcome.FAILED, retryable=False, reason=reason), now
        )


class ModelCacheStore:
    """``LifecycleStore`` for the reconciler: the operations that need a decision."""

    def __init__(
        self,
        scope: Callable[..., AbstractContextManager[Session]],
        adapter: ModelCacheAdapter,
        effects: CacheEffects,
    ) -> None:
        self._scope = scope
        self._adapter = adapter
        self._effects = effects

    def due(self, now: datetime, limit: int) -> Sequence[Lifecycle]:
        """Active operations whose clock, lease or cancel asks for a decision."""

        now = aware(now)
        here = self._effects.running_here()
        due: list[Lifecycle] = []
        with self._scope() as session:
            rows = session.scalars(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.state.in_(ACTIVE_STORED_STATES))
                .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                .limit(SWEEP_LIMIT)
            )
            for operation in rows:
                row = self._adapter.lifecycle(operation, now)
                if row.terminal or operation.id in here:
                    continue
                if operation.kind == "remove":
                    continue  # advanced step by step by the removal worker
                if row.cancel_requested:
                    wanted = row.next_action_at is None or row.next_action_at <= now
                elif row.state is State.RUNNING:
                    wanted = (
                        row.lease_deadline is not None and row.lease_deadline <= now
                    )
                elif row.state in (State.OBSERVING, State.NEEDS_OPERATOR):
                    wanted = row.next_action_at is None or row.next_action_at <= now
                else:
                    # A due retry is claimed by the transfer pool, not here.
                    wanted = False
                if wanted:
                    due.append(row)
                    if len(due) >= limit:
                        break
        return due

    def save(self, before: Lifecycle, after: Lifecycle) -> bool:
        """Persist a decision under the row's lock, if the row is still ``before``."""

        now = self._adapter.now()
        with self._scope(write=True) as session:
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == before.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if operation is None:
                return False
            current = self._adapter.lifecycle(operation, now)
            if not same_order(before, current):
                return False
            self._adapter.apply(operation, before, after, now)
        return True

    def record_residue(self, row: Lifecycle, reason: str) -> None:
        with self._scope(write=True) as session:
            operation = session.get(ModelCacheOperation, row.id)
            if operation is not None:
                operation.last_error = reason[:_MAX_REASON]


def adopt_legacy_operations(connection: Connection) -> int:
    """Startup adoption: move the legacy retry clock and claim onto the columns.

    Idempotent and bounded to active operations whose columns are still empty.
    A live claim of another process keeps its operation ``running`` under that
    process's lease (never touched); an expired one is left for the core to
    lapse.  Everything this misses is adopted lazily by
    :meth:`ModelCacheAdapter.lifecycle`, so a gap heals instead of stranding a row.
    """

    rows = connection.execute(
        select(
            ModelCacheOperation.id,
            ModelCacheOperation.state,
            ModelCacheOperation.payload,
        )
        .where(
            ModelCacheOperation.state.in_(ACTIVE_STORED_STATES),
            ModelCacheOperation.next_action_at.is_(None),
            ModelCacheOperation.lease_deadline.is_(None),
        )
        .limit(SWEEP_LIMIT * 10)
    ).all()
    now = datetime.now(UTC)
    adopted = 0
    for row_id, state, payload in rows:
        if not isinstance(payload, Mapping):
            continue
        values: dict[str, Any] = {}
        claim = legacy_claim(payload)
        if claim is not None:
            owner, expires = claim
            values.update(fence=owner[:64], lease_deadline=expires)
            if expires > now and state != "running":
                values["state"] = "running"
        due = legacy_retry_due(payload)
        if due is not None and "state" not in values:
            values["next_action_at"] = due
        if not values:
            continue
        connection.execute(
            update(ModelCacheOperation)
            .where(ModelCacheOperation.id == row_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        adopted += 1
    return adopted


__all__ = [
    "ACTIVE_STORED_STATES",
    "CacheEffects",
    "ModelCacheAdapter",
    "ModelCacheStore",
    "adopt_legacy_operations",
    "cancellation_of",
    "legacy_claim",
    "legacy_retry_due",
]
