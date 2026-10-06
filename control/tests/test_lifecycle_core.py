"""Exhaustive behavior table for the shared lifecycle core.

``transition`` is a pure function of ``(row, event, adapter, now)``, so its
contract is checked as a table over every state, event, effect and kind property
rather than by hand-picked cases: totality, the rules of the blocker audit
(section 5.2) as invariants over the whole table, and a simulated clock for the
two liveness promises (a cancel completes; an unknown effect never parks a row
without an action).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from vonk_control.lifecycle import (
    OBSERVE_BUDGET,
    STOP_BUDGET,
    TERMINAL_STATES,
    CancelRequested,
    Claimed,
    Decision,
    Dispatch,
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
    Reconciler,
    RecordResidue,
    Reported,
    State,
    Stop,
    StopResult,
    Submitted,
    Tick,
    transition,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


@dataclass
class FakeAdapter:
    """A kind whose two pure facts are configured; effects are scripted."""

    kind: str = "fake"
    is_irreversible: bool = False
    advertised: tuple[str, ...] = ()
    observed: Effect = Effect.UNKNOWN
    stop_result: StopResult = StopResult.UNCONFIRMED
    fence_until: datetime | None = None
    calls: list[str] = field(default_factory=list)

    def adopt(self, stored: Lifecycle) -> Lifecycle:
        return stored

    def irreversible(self, row: Lifecycle) -> bool:
        return self.is_irreversible

    def retry_not_before(self, row: Lifecycle, now: datetime) -> datetime | None:
        return self.fence_until

    def execute(self, row: Lifecycle, attempt: int) -> Dispatch:
        self.calls.append("execute")
        return Dispatch()

    def observe(self, row: Lifecycle) -> Observed:
        self.calls.append("observe")
        return Observed(self.observed)

    def stop(self, row: Lifecycle) -> StopResult:
        self.calls.append("stop")
        return self.stop_result

    def actions(self, row: Lifecycle) -> tuple[str, ...]:
        return self.advertised

    def children(self, row: Lifecycle) -> tuple[Lifecycle, ...]:
        return ()


def _row(**fields: object) -> Lifecycle:
    base = Lifecycle(
        id="op-1", kind="fake", fence="f1", attempt=1, effect=Effect.ISSUED
    )
    return replace(base, **fields)  # type: ignore[arg-type]


EVENTS: tuple[Event, ...] = (
    Submitted(),
    Claimed(attempt=2, fence="f2", lease_deadline=LATER),
    Claimed(attempt=1, fence="f2", lease_deadline=LATER),
    Heartbeat(fence="f1", lease_deadline=LATER + timedelta(minutes=1)),
    Heartbeat(fence="other", lease_deadline=LATER + timedelta(minutes=1)),
    *(
        Reported(outcome, fence="f1", retryable=retryable, effect=effect)
        for outcome in Outcome
        for retryable in (False, True)
        for effect in (None, Effect.NONE, Effect.STOPPED, Effect.ESTABLISHED)
    ),
    Reported(Outcome.UNKNOWN, fence="other"),
    LeaseLapsed(),
    CancelRequested(request_key="k"),
    *(Observed(effect) for effect in Effect),
    OperatorAction("resume"),
    OperatorAction("retry"),
    OperatorAction("retire"),
    OperatorAction("stop"),
    OperatorAction("unknown-action"),
    Tick(),
)
ADAPTERS = tuple(
    FakeAdapter(is_irreversible=irreversible, advertised=advertised)
    for irreversible in (False, True)
    for advertised in ((), ("resume", "retire", "stop"))
)


def _rows() -> Iterator[Lifecycle]:
    for state, attempt, effect, cancelled, due, observed in itertools.product(
        State,
        (0, 1, 3),
        Effect,
        (False, True),
        (None, NOW - timedelta(seconds=1), NOW + timedelta(hours=2)),
        (0, OBSERVE_BUDGET),
    ):
        yield _row(
            state=state,
            attempt=attempt,
            effect=effect,
            cancel_requested_at=NOW - timedelta(minutes=1) if cancelled else None,
            cancel_request_key="k0" if cancelled else None,
            next_action_at=due,
            observe_count=observed,
            lease_deadline=NOW + timedelta(minutes=1),
        )


def _table() -> Iterator[tuple[Lifecycle, Event, FakeAdapter, Decision]]:
    for row in _rows():
        for event in EVENTS:
            for adapter in ADAPTERS:
                yield row, event, adapter, transition(row, event, adapter, NOW)


def _describe(row: Lifecycle, event: Event, adapter: FakeAdapter) -> str:
    return (
        f"state={row.state} attempt={row.attempt} effect={row.effect} "
        f"cancel={row.cancel_requested} due={row.next_action_at} "
        f"observed={row.observe_count} event={event} "
        f"irreversible={adapter.is_irreversible} actions={adapter.advertised}"
    )


def test_transition_is_total_and_keeps_identity() -> None:
    cases = 0
    for row, event, adapter, decision in _table():
        cases += 1
        assert isinstance(decision, Decision), _describe(row, event, adapter)
        assert decision.row.id == row.id and decision.row.kind == row.kind
        assert isinstance(decision.row.state, State)
    assert cases > 50_000


@pytest.mark.usefixtures("damaged_json_rows")
def test_terminal_rows_never_move_and_a_cancel_flag_never_clears() -> None:
    for row, event, adapter, decision in _table():
        where = _describe(row, event, adapter)
        if row.terminal:
            assert decision == Decision(row), where
        if row.cancel_requested:
            assert decision.row.cancel_requested_at == row.cancel_requested_at, where
            assert decision.row.cancel_request_key == row.cancel_request_key, where


def test_never_needs_operator_without_an_advertised_action() -> None:
    """Rule 3, over the whole table."""

    parked = 0
    for row, event, adapter, decision in _table():
        if decision.row.state is not State.NEEDS_OPERATOR:
            continue
        where = _describe(row, event, adapter)
        if row.state is State.NEEDS_OPERATOR and decision.row == row:
            continue  # an input row left alone; it is only re-evaluated by a Tick
        parked += 1
        assert adapter.is_irreversible, where
        assert adapter.advertised, where
        assert decision.row.effect in {Effect.UNKNOWN, Effect.ISSUED}, where
        assert decision.row.attempt > 0, where
    assert parked > 0  # the table does reach the state it guards


def test_an_unadvertised_operator_action_changes_nothing() -> None:
    for adapter in ADAPTERS:
        for row in _rows():
            for name in ("resume", "retry", "retire", "stop"):
                if name in adapter.advertised or row.terminal:
                    continue
                assert transition(row, OperatorAction(name), adapter, NOW) == Decision(
                    row
                )


def test_never_executed_or_idempotent_work_is_always_retried() -> None:
    """Rule 1: a lapsed lease, an uncertain report or a retryable failure."""

    uncertain: tuple[Event, ...] = (
        LeaseLapsed(),
        Reported(Outcome.UNKNOWN, fence="f1"),
        Reported(Outcome.FAILED, fence="f1", retryable=True),
    )
    checked = 0
    for adapter in ADAPTERS:
        for attempt, effect, irreversible in itertools.product(
            (0, 1, 3), Effect, (False, True)
        ):
            adapter = replace(adapter, is_irreversible=irreversible)
            row = _row(
                state=State.RUNNING,
                attempt=attempt,
                effect=effect,
                lease_deadline=NOW - timedelta(seconds=1),
                next_action_at=NOW,
            )
            if not (attempt == 0 or effect is Effect.NONE or not irreversible):
                continue
            for event in uncertain:
                decision = transition(row, event, adapter, NOW)
                where = _describe(row, event, adapter)
                assert decision.row.state is State.BACKOFF, where
                assert decision.row.next_action_at is not None, where
                assert decision.row.next_action_at > NOW, where
                assert decision.row.retry_count == row.retry_count + 1, where
                assert decision.commands == (), where
                checked += 1
    assert checked > 0


def test_parked_legacy_rows_are_reevaluated_on_their_first_tick() -> None:
    """A legacy ``waiting-for-operator`` row maps to ``needs-operator``."""

    idempotent = FakeAdapter(is_irreversible=False)
    parked = _row(state=State.NEEDS_OPERATOR, next_action_at=NOW, effect=Effect.UNKNOWN)
    healed = transition(parked, Tick(), idempotent, NOW)
    assert healed.row.state is State.BACKOFF
    assert healed.row.next_action_at is not None and healed.row.next_action_at > NOW

    never = transition(
        replace(parked, attempt=0), Tick(), FakeAdapter(is_irreversible=True), NOW
    )
    assert never.row.state is State.BACKOFF

    no_action = FakeAdapter(is_irreversible=True, advertised=())
    observing = transition(parked, Tick(), no_action, NOW)
    assert observing.row.state is State.OBSERVING
    assert observing.commands == (Observe(),)

    with_action = FakeAdapter(is_irreversible=True, advertised=("resume",))
    kept = transition(parked, Tick(), with_action, NOW)
    assert kept.row.state is State.NEEDS_OPERATOR and kept.row.next_action_at is None


def test_an_irreversible_uncertain_effect_is_observed_first() -> None:
    """Rule 2."""

    adapter = FakeAdapter(is_irreversible=True, advertised=("resume",))
    running = _row(state=State.RUNNING, lease_deadline=NOW - timedelta(seconds=1))
    lapsed = transition(running, LeaseLapsed(), adapter, NOW)
    assert lapsed.row.state is State.OBSERVING
    assert lapsed.commands == (Observe(),)

    observing = lapsed.row
    done = transition(observing, Observed(Effect.ESTABLISHED), adapter, NOW)
    assert done.row.state is State.SUCCEEDED
    retried = transition(observing, Observed(Effect.NONE), adapter, NOW)
    assert retried.row.state is State.BACKOFF and retried.row.effect is Effect.NONE
    stopped = transition(observing, Observed(Effect.STOPPED), adapter, NOW)
    assert stopped.row.state is State.BACKOFF
    unknown = transition(observing, Observed(Effect.UNKNOWN), adapter, NOW)
    assert unknown.row.state is State.OBSERVING


def _tick_until(
    row: Lifecycle, adapter: FakeAdapter, ticks: int, answer: Effect | None
) -> Iterator[Lifecycle]:
    """Drive ``row`` with a clock that always reaches the next action."""

    now = NOW
    for _ in range(ticks):
        now = max(now, row.next_action_at or now) + timedelta(seconds=1)
        decision = transition(row, Tick(), adapter, now)
        row = decision.row
        for command in decision.commands:
            if answer is not None and isinstance(command, (Observe, Stop)):
                row = transition(row, Observed(answer), adapter, now).row
        yield row


def test_an_unknown_effect_waits_for_an_operator_only_with_an_action() -> None:
    row = _row(state=State.OBSERVING, next_action_at=NOW, observe_count=0)
    with_action = FakeAdapter(is_irreversible=True, advertised=("resume", "retire"))
    states = [r.state for r in _tick_until(row, with_action, 40, Effect.UNKNOWN)]
    assert State.NEEDS_OPERATOR in states
    assert states[-1] is State.NEEDS_OPERATOR

    without = FakeAdapter(is_irreversible=True, advertised=())
    states = [r.state for r in _tick_until(row, without, 200, Effect.UNKNOWN)]
    assert set(states) == {State.OBSERVING}  # observed forever, at a bounded rate


def test_an_operator_chooses_among_the_advertised_actions() -> None:
    adapter = FakeAdapter(is_irreversible=True, advertised=("resume", "retire", "stop"))
    waiting = _row(state=State.NEEDS_OPERATOR, effect=Effect.UNKNOWN, retry_count=4)
    resumed = transition(waiting, OperatorAction("resume"), adapter, NOW).row
    assert resumed.state is State.QUEUED and resumed.retry_count == 0
    retired = transition(waiting, OperatorAction("retire"), adapter, NOW).row
    assert retired.state is State.FAILED and retired.effect is Effect.UNKNOWN
    stopped = transition(waiting, OperatorAction("stop"), adapter, NOW)
    assert stopped.row.cancel_requested and stopped.commands == (Stop(),)


def test_a_stale_fence_is_dropped_and_the_current_one_is_accepted() -> None:
    adapter = FakeAdapter()
    running = _row(state=State.RUNNING)
    stale = Reported(Outcome.DONE, fence="old")
    assert transition(running, stale, adapter, NOW) == Decision(running)
    assert transition(running, Heartbeat("old", LATER), adapter, NOW) == Decision(
        running
    )
    fresh = transition(running, Reported(Outcome.DONE, fence="f1"), adapter, NOW)
    assert fresh.row.state is State.SUCCEEDED and fresh.row.effect is Effect.ESTABLISHED
    renewed = transition(running, Heartbeat("f1", LATER), adapter, NOW).row
    assert renewed.lease_deadline == LATER and renewed.next_action_at == LATER


def test_a_claim_starts_an_attempt_only_for_claimable_work() -> None:
    adapter = FakeAdapter()
    queued = _row(state=State.QUEUED, attempt=0, effect=Effect.NONE)
    claimed = transition(queued, Claimed(1, "f9", LATER), adapter, NOW).row
    assert (claimed.state, claimed.attempt, claimed.fence) == (State.RUNNING, 1, "f9")
    backing_off = _row(state=State.BACKOFF, next_action_at=LATER)
    assert transition(backing_off, Claimed(2, "f9", LATER), adapter, NOW).row == (
        backing_off
    )
    cancelled = replace(queued, cancel_requested_at=NOW)
    assert transition(cancelled, Claimed(1, "f9", LATER), adapter, NOW).row == cancelled


def test_a_tick_that_is_not_due_changes_nothing() -> None:
    adapter = FakeAdapter(is_irreversible=True, advertised=("resume",))
    for state in State:
        row = _row(state=state, next_action_at=NOW + timedelta(minutes=5))
        assert transition(row, Tick(), adapter, NOW) == Decision(row)


# ------------------------------------------------------------------- cancel


class MemoryStore:
    def __init__(self, rows: Sequence[Lifecycle]) -> None:
        self.rows = {row.id: row for row in rows}
        self.residues: list[tuple[str, str]] = []
        self.now = NOW

    def due(self, now: datetime, limit: int) -> Sequence[Lifecycle]:
        due = [
            row
            for row in self.rows.values()
            if not row.terminal
            and row.next_action_at is not None
            and row.next_action_at <= now
        ]
        return sorted(due, key=lambda row: row.next_action_at or now)[:limit]

    def save(self, before: Lifecycle, after: Lifecycle) -> bool:
        if self.rows[before.id] != before:
            return False
        self.rows[after.id] = after
        return True

    def record_residue(self, row: Lifecycle, reason: str) -> None:
        self.residues.append((row.id, reason))


@pytest.mark.parametrize("irreversible", [False, True])
@pytest.mark.parametrize("state", [s for s in State if s not in TERMINAL_STATES])
@pytest.mark.parametrize("attempt", [0, 1])
@pytest.mark.parametrize("effect", list(Effect))
@pytest.mark.parametrize(
    ("observed", "stop_result"),
    [
        (Effect.UNKNOWN, StopResult.UNCONFIRMED),
        (Effect.ESTABLISHED, StopResult.UNCONFIRMED),
        (Effect.UNKNOWN, StopResult.CONFIRMED),
    ],
)
def test_a_cancel_reaches_a_terminal_state_within_the_stop_budget(
    irreversible: bool,
    state: State,
    attempt: int,
    effect: Effect,
    observed: Effect,
    stop_result: StopResult,
) -> None:
    """Rule 4: dead work, silent executors and stops that never confirm."""

    adapter = FakeAdapter(
        is_irreversible=irreversible,
        advertised=("stop",),
        observed=observed,
        stop_result=stop_result,
    )
    row = _row(
        state=state,
        attempt=attempt,
        effect=effect,
        next_action_at=NOW,
        lease_deadline=NOW - timedelta(minutes=5),
    )
    store = MemoryStore([row])
    clock = [NOW]
    reconciler = Reconciler(
        store, {"fake": adapter}, clock=lambda: clock[0], enabled=True
    )
    first = transition(row, CancelRequested(request_key="k"), adapter, NOW)
    store.save(row, first.row)
    seen = [first.row.state]
    for command in first.commands:
        if isinstance(command, Stop):
            confirmed = stop_result is StopResult.CONFIRMED
            before = store.rows[row.id]
            store.save(
                before,
                transition(
                    before,
                    Observed(Effect.STOPPED if confirmed else Effect.UNKNOWN),
                    adapter,
                    NOW,
                ).row,
            )
            seen.append(store.rows[row.id].state)
    ticks = 0
    while not store.rows[row.id].terminal:
        ticks += 1
        assert ticks <= STOP_BUDGET + 2, f"cancel did not complete: {seen}"
        clock[0] += timedelta(minutes=2)
        reconciler.reconcile()
        seen.append(store.rows[row.id].state)
    final = store.rows[row.id]
    assert final.state is State.CANCELLED
    assert State.NEEDS_OPERATOR not in seen
    assert State.QUEUED not in seen[1:] and State.BACKOFF not in seen[1:]
    if final.effect is Effect.UNKNOWN and attempt > 0 and effect is not Effect.NONE:
        confirmed_stop = stop_result is StopResult.CONFIRMED
        assert confirmed_stop or store.residues  # the residue is recorded


def test_a_cancel_of_never_executed_work_is_immediate_and_repeats_are_harmless() -> (
    None
):
    adapter = FakeAdapter(is_irreversible=True)
    queued = _row(state=State.QUEUED, attempt=0, effect=Effect.NONE)
    decision = transition(queued, CancelRequested("k"), adapter, NOW)
    assert decision.row.state is State.CANCELLED and decision.commands == ()
    again = transition(decision.row, CancelRequested("k2"), adapter, NOW)
    assert again == Decision(decision.row)

    running = _row(state=State.RUNNING)
    first = transition(running, CancelRequested("k"), adapter, NOW)
    assert first.row.state is State.OBSERVING and first.commands == (Stop(),)
    second = transition(first.row, CancelRequested("k2"), adapter, NOW)
    assert second == Decision(first.row)  # the first request key is kept
    assert second.row.cancel_request_key == "k"


def test_a_cancel_gives_up_with_a_residue_after_the_budget() -> None:
    adapter = FakeAdapter(is_irreversible=True, advertised=("stop",))
    row = _row(state=State.RUNNING)
    decision = transition(row, CancelRequested("k"), adapter, NOW)
    now = NOW
    for _ in range(STOP_BUDGET + 1):
        if decision.row.terminal:
            break
        now = max(now, decision.row.next_action_at or now) + timedelta(seconds=1)
        decision = transition(decision.row, Tick(), adapter, now)
    assert decision.row.state is State.CANCELLED
    assert decision.row.effect is Effect.UNKNOWN
    assert any(isinstance(command, RecordResidue) for command in decision.commands)


# --------------------------------------------------------------- reconciler


def test_the_reconciler_is_disabled_by_default() -> None:
    adapter = FakeAdapter()
    row = _row(state=State.OBSERVING, next_action_at=NOW)
    store = MemoryStore([row])
    reconciler = Reconciler(store, {"fake": adapter}, clock=lambda: LATER)
    assert reconciler.enabled is False
    report = reconciler.reconcile()
    assert (report.examined, report.changed) == (0, 0)
    assert store.rows[row.id] == row and adapter.calls == []


def test_the_reconciler_heals_a_parked_row_and_ignores_unknown_kinds() -> None:
    adapter = FakeAdapter(is_irreversible=False)
    parked = _row(state=State.NEEDS_OPERATOR, next_action_at=NOW, effect=Effect.UNKNOWN)
    stranger = replace(parked, id="op-2", kind="not-migrated")
    store = MemoryStore([parked, stranger])
    reconciler = Reconciler(store, {"fake": adapter}, clock=lambda: LATER, enabled=True)
    report = reconciler.reconcile()
    assert report.examined == 1
    assert store.rows["op-1"].state is State.BACKOFF
    assert store.rows["op-2"] == stranger
    # the retry is claimed from the queue after its backoff; a second pass is a
    # no-op until then
    again = reconciler.reconcile()
    assert again.changed == 0


def test_the_reconciler_runs_observations_and_feeds_the_answer_back() -> None:
    adapter = FakeAdapter(is_irreversible=True, advertised=("resume",))
    adapter.observed = Effect.ESTABLISHED
    row = _row(state=State.OBSERVING, next_action_at=NOW)
    store = MemoryStore([row])
    Reconciler(store, {"fake": adapter}, clock=lambda: LATER, enabled=True).reconcile()
    assert adapter.calls == ["observe"]
    assert store.rows[row.id].state is State.SUCCEEDED


def test_an_executor_failure_is_an_unknown_not_an_exception() -> None:
    class Broken(FakeAdapter):
        def observe(self, row: Lifecycle) -> Observed:
            raise RuntimeError("helper unreachable")

    adapter = Broken(is_irreversible=True, advertised=("resume",))
    row = _row(state=State.OBSERVING, next_action_at=NOW)
    store = MemoryStore([row])
    Reconciler(store, {"fake": adapter}, clock=lambda: LATER, enabled=True).reconcile()
    assert store.rows[row.id].state is State.OBSERVING
    assert Execute() == Execute()


def test_a_definite_report_ends_the_row_even_when_a_cancel_is_pending() -> None:
    """A build whose completion raced its cancel must show its real outcome."""

    adapter = FakeAdapter(is_irreversible=True, advertised=("stop",))
    running = _row(state=State.RUNNING, cancel_requested_at=NOW, cancel_request_key="k")
    for outcome, expected in (
        (Outcome.DONE, State.SUCCEEDED),
        (Outcome.CANCELLED, State.CANCELLED),
        (Outcome.FAILED, State.FAILED),
    ):
        decision = transition(running, Reported(outcome, fence="f1"), adapter, NOW)
        assert decision.row.state is expected and decision.commands == ()
        assert decision.row.cancel_requested_at == NOW
    uncertain = transition(running, Reported(Outcome.UNKNOWN, fence="f1"), adapter, NOW)
    assert uncertain.row.state is State.OBSERVING  # the cancel path, not a retry


def test_a_retry_never_starts_before_the_kinds_safety_fence() -> None:
    fence = NOW + timedelta(seconds=960)
    adapter = FakeAdapter(is_irreversible=False, fence_until=fence)
    running = _row(state=State.RUNNING, lease_deadline=NOW - timedelta(seconds=1))
    decision = transition(running, LeaseLapsed(), adapter, NOW)
    assert decision.row.state is State.BACKOFF
    assert decision.row.next_action_at is not None
    assert decision.row.next_action_at >= fence
