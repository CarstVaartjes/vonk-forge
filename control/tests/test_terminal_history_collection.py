from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control import terminal_history_collection
from vonk_control.catalog_revision_collection import INTERVAL
from vonk_control.models import (
    Base,
    Job,
    JobAttempt,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_execution_contract import StoredRunNodePlan, StoredRunPlan
from vonk_control.terminal_history_collection import (
    BACKLOG_PAUSE,
    TerminalHistoryCollector,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def history_sessions(request: pytest.FixtureRequest) -> Iterator[sessionmaker[Session]]:
    engine = (
        create_engine("sqlite:///:memory:")
        if request.param == "sqlite"
        else request.getfixturevalue("postgres_engine")
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    if request.param == "sqlite":
        engine.dispose()


@dataclass
class InventoryElapsed:
    seconds: float = 0.0
    queries: int = 0

    def monotonic(self) -> float:
        return self.seconds


@pytest.fixture
def slow_job_inventory(
    history_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[InventoryElapsed]:
    """Charge elapsed time to real SQL inventory, without sleeping or changing it."""
    engine = history_sessions.kw["bind"]
    assert isinstance(engine, Engine)
    elapsed = InventoryElapsed()

    def charge_inventory(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        if statement.startswith("SELECT jobs.id, jobs.updated_at"):
            elapsed.seconds += 2.1
            elapsed.queries += 1

    monkeypatch.setattr(
        terminal_history_collection,
        "time",
        SimpleNamespace(monotonic=elapsed.monotonic),
    )
    event.listen(engine, "after_cursor_execute", charge_inventory)
    try:
        yield elapsed
    finally:
        event.remove(engine, "after_cursor_execute", charge_inventory)


def _job(
    now: datetime, *, state: str = "succeeded", payload: dict[str, object] | None = None
) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="history-test",
        state=state,
        actor="operator",
        authority_revision="revision",
        targets=[],
        payload_digest="a" * 64,
        payload=payload or {},
        current_attempt=0,
        created_at=now,
        updated_at=now,
    )


def _stopped_run(now: datetime, node_id: str) -> RecipeRun:
    installation_id, mapping_id, revision_id = (str(uuid.uuid4()) for _ in range(3))
    plan = StoredRunPlan(
        schema_version=1,
        observation_schema_version=2,
        run_generation=2,
        installation_id=installation_id,
        alias="demo",
        mapping_id=mapping_id,
        mapping_generation=1,
        recipe_revision_id=revision_id,
        plan_digest="a" * 64,
        nodes=[
            StoredRunNodePlan(
                node_id=node_id,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                port=8000,
                allowed=True,
                inventory_observed_at=None,
                memory_kind="unified",
                memory_pool="shared",
                required_memory_bytes=1,
                available_memory_bytes=None,
                active_reserved_bytes=0,
                free_after_bytes=None,
                memory_floor_bytes=0,
                fabric_address=None,
                fabric_bandwidth_mbps=None,
                rendezvous_port=None,
                blockers=[],
                warnings=[],
            )
        ],
    )
    return RecipeRun(
        id=str(uuid.uuid4()),
        installation_id=installation_id,
        mapping_id=mapping_id,
        mapping_generation=1,
        run_generation=2,
        alias="demo",
        plan_digest="a" * 64,
        plan=plan.model_dump(mode="json"),
        state="stopped",
        actor="operator",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("last_word", ["never-launched", "never-observed", "running"])
def test_ended_run_history_is_reclaimed_whatever_the_spark_last_said(
    last_word: str,
) -> None:
    """A stopped run's observations are rejected and its disposition is
    `unowned`, exactly as for a missing row: no absence proof can arrive, so
    waiting for one would keep the row forever."""
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_id = "spk_" + "0" * 32
    run = _stopped_run(old, node_id)
    kept = _stopped_run(old, node_id)
    try:
        with sessions.begin() as session:
            session.add_all((run, kept))
            session.flush()
            if last_word != "never-launched":
                session.add(
                    RunNode(
                        run_id=run.id,
                        node_id=node_id,
                        rank=0,
                        role="entrypoint",
                        state="stopped",
                        port=8000,
                        reserved_memory_bytes=1,
                        observed_run_generation=(2 if last_word == "running" else None),
                        observation_process_running=(
                            True if last_word == "running" else None
                        ),
                        observation_observed_at=(
                            old if last_word == "running" else None
                        ),
                        updated_at=old,
                    )
                )
            # A real consumer still retains its run.
            session.add(
                ResourceReservation(
                    node_id=node_id,
                    kind="unified-memory",
                    resource_key="unified-memory",
                    amount_bytes=1,
                    owner_kind="run",
                    owner_id=kept.id,
                    state="active",
                    plan_digest="a" * 64,
                    created_at=old,
                )
            )
        collector = TerminalHistoryCollector(sessions, clock=lambda: now)
        assert collector.collect()["recipe_runs"] == 1
        with sessions() as session:
            assert session.get(RecipeRun, run.id) is None
            assert session.scalar(select(RunNode.id)) is None
            assert session.get(RecipeRun, kept.id) is not None
    finally:
        engine.dispose()


def test_one_pass_judges_every_row_against_one_reference_inventory(
    history_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        terminal_history_collection, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    now = datetime.now(UTC)
    rows = [_job(now - timedelta(days=2)) for _ in range(8)]
    with history_sessions.begin() as session:
        session.add_all(rows)
    engine = history_sessions.kw["bind"]
    assert isinstance(engine, Engine)
    reference_scans: list[str] = []

    def count_reference_scans(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        # Only the reference inventory reads builds; none is per row.
        if "FROM recipe_builds" in statement:
            reference_scans.append(statement)

    event.listen(engine, "after_cursor_execute", count_reference_scans)
    try:
        with history_sessions() as session:
            assert (
                terminal_history_collection._protected_tokens(session, now) is not None
            )
        one_inventory = len(reference_scans)
        reference_scans.clear()
        # Small batches: the backlog drains within the pass, cycle by cycle.
        collector = TerminalHistoryCollector(
            history_sessions, clock=lambda: now, batch=2
        )
        assert collector.collect()["jobs"] == len(rows)
    finally:
        event.remove(engine, "after_cursor_execute", count_reference_scans)
    assert one_inventory and len(reference_scans) == one_inventory
    with history_sessions() as session:
        assert session.scalar(select(Job.id)) is None


def test_an_exhausted_pass_continues_after_a_short_pause_not_the_next_tick(
    history_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live regression (2026-10-17): a backlog drained one batch an hour.

    An hourly two-second pass removed about 36 rows an hour, so a backlog of
    thousands of ended rows outlived the 24 h grace by days. A pass that runs
    out of budget with rows left continues after a short pause; each pass is
    still bounded, and a finished cycle waits the whole interval.
    """
    elapsed = InventoryElapsed()
    monkeypatch.setattr(
        terminal_history_collection,
        "time",
        SimpleNamespace(monotonic=elapsed.monotonic),
    )
    clock = [datetime.now(UTC)]
    first, second = (_job(clock[0] - timedelta(days=2)) for _ in range(2))
    with history_sessions.begin() as session:
        session.add_all((first, second))
    engine = history_sessions.kw["bind"]
    assert isinstance(engine, Engine)

    def charge_deletion(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        if statement.startswith("DELETE FROM jobs"):
            elapsed.seconds += 2.1

    def remaining() -> set[str]:
        with history_sessions() as session:
            return set(session.scalars(select(Job.id)))

    collector = TerminalHistoryCollector(
        history_sessions, clock=lambda: clock[0], batch=1
    )
    event.listen(engine, "after_cursor_execute", charge_deletion)
    try:
        assert collector.tick()  # A full batch and a spent budget.
        assert len(remaining()) == 1
        clock[0] += timedelta(seconds=1)
        assert not collector.tick()
        assert len(remaining()) == 1
        clock[0] += BACKLOG_PAUSE
        assert collector.tick()
        assert remaining() == set()
        # The cycle has finished: the next pass waits the whole interval.
        assert collector.tick() is False
        clock[0] += BACKLOG_PAUSE
        assert collector.tick() is False
        clock[0] += INTERVAL
        assert collector.tick() is False
    finally:
        event.remove(engine, "after_cursor_execute", charge_deletion)


def test_a_backlog_drains_within_minutes_not_hours(
    history_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = InventoryElapsed()
    monkeypatch.setattr(
        terminal_history_collection,
        "time",
        SimpleNamespace(monotonic=elapsed.monotonic),
    )
    clock = [datetime.now(UTC)]
    with history_sessions.begin() as session:
        session.add_all(_job(clock[0] - timedelta(days=2)) for _ in range(12))
    engine = history_sessions.kw["bind"]
    assert isinstance(engine, Engine)

    def charge_deletion(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        # Each deletion spends half a pass: two rows per pass.
        if statement.startswith("DELETE FROM jobs"):
            elapsed.seconds += 1.0

    collector = TerminalHistoryCollector(
        history_sessions, clock=lambda: clock[0], batch=4
    )
    event.listen(engine, "after_cursor_execute", charge_deletion)
    try:
        start = clock[0]
        for _ in range(12):
            collector.tick()
            clock[0] += BACKLOG_PAUSE
    finally:
        event.remove(engine, "after_cursor_execute", charge_deletion)
    with history_sessions() as session:
        assert session.scalar(select(Job.id)) is None
    assert clock[0] - start < INTERVAL


def test_terminal_history_prunes_old_rows_but_preserves_live_and_recent_references(
    history_sessions: sessionmaker[Session],
    slow_job_inventory: InventoryElapsed,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    removable = _job(old)
    referenced = _job(old)
    # Equal timestamps use identity order; recheck the reference before deletion.
    referenced.id = str(uuid.UUID(int=1))
    removable.id = str(uuid.UUID(int=2))
    recent = _job(now)
    live = _job(old, state="running", payload={"operation_id": referenced.id})
    with history_sessions.begin() as session:
        session.add_all((removable, referenced, recent, live))
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    counts: Counter[str] = Counter()
    protected = {referenced.id, recent.id, live.id}
    # Successful inventory consumes this pass. Its bounded observation must
    # survive the yield; the next pass rechecks each row before deleting it.
    for expected_removed in (0, 1):
        counts.update(collector.collect())
        assert counts["jobs"] == expected_removed
        with history_sessions() as session:
            remaining = set(session.scalars(select(Job.id)))
            assert protected <= remaining
            assert remaining <= protected | {removable.id}
    assert counts["jobs"] == 1
    assert slow_job_inventory.queries == 1
    with history_sessions() as session:
        assert session.get(Job, removable.id) is None
        assert set(session.scalars(select(Job.id))) == {
            referenced.id,
            recent.id,
            live.id,
        }


def test_terminal_parent_with_unreconciled_attempt_is_kept_until_attempt_ends(
    history_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This checks the attempt's lifecycle, not how many passes a loaded host
    # needs. Budget exhaustion and continuation have separate elapsed-clock tests.
    elapsed = InventoryElapsed()
    monkeypatch.setattr(
        terminal_history_collection,
        "time",
        SimpleNamespace(monotonic=elapsed.monotonic),
    )
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    job = _job(old, state="failed")
    attempt_id = str(uuid.uuid4())
    with history_sessions.begin() as session:
        session.add(job)
        session.flush()
        session.add(
            JobAttempt(
                id=attempt_id,
                job_id=job.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                worker_id="worker",
                lease_deadline=old,
                state="running",
            )
        )
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    with history_sessions.begin() as session:
        attempt = session.get(JobAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "failed"
    assert (collector.collect() + collector.collect())["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, job.id) is None
        assert session.get(JobAttempt, attempt_id) is None


def test_unreadable_live_authority_defers_pruning_until_repaired(
    history_sessions: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    removable = _job(old)
    unreadable = _job(now, state="running")
    # Empty documents are allowed fixture placeholders; the canonical reader
    # correctly reports this as a missing required recipe-operation document.
    unreadable.kind = "recipe.start"
    with history_sessions.begin() as session:
        session.add_all((removable, unreadable))
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    with history_sessions.begin() as session:
        assert session.get(Job, removable.id) is not None
        owner = session.get(Job, unreadable.id)
        assert owner is not None
        owner.kind = "history-test"
    assert collector.collect()["jobs"] == 1


def test_retained_oldest_rows_do_not_starve_later_unreferenced_history(
    history_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This tests cursor order, not how much SQL fits in a wall-clock pass.
    monkeypatch.setattr(
        terminal_history_collection, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    retained = _job(now - timedelta(days=3))
    removable = _job(now - timedelta(days=2))
    live = _job(now, state="running", payload={"operation_id": retained.id})
    with history_sessions.begin() as session:
        session.add_all((retained, removable, live))
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now, batch=1)
    assert collector.collect()["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, retained.id) is not None
        assert session.get(Job, removable.id) is None


@pytest.mark.parametrize(
    "change", ["reference", "recent", "cancelled-with-live-attempt"]
)
def test_inventory_budget_continuation_rechecks_current_authority(
    history_sessions: sessionmaker[Session],
    slow_job_inventory: InventoryElapsed,
    change: str,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    candidate = _job(old)
    with history_sessions.begin() as session:
        session.add(candidate)
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    assert slow_job_inventory.queries == 1
    with history_sessions.begin() as session:
        row = session.get(Job, candidate.id)
        assert row is not None
        if change == "reference":
            session.add(_job(now, state="running", payload={"operation_id": row.id}))
        elif change == "recent":
            row.updated_at = now
        else:
            row.state = "cancelled"
            session.add(
                JobAttempt(
                    id=str(uuid.uuid4()),
                    job_id=row.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    worker_id="worker",
                    lease_deadline=old,
                    state="running",
                )
            )
    assert not collector.collect()
    # A cached observation is not permission to delete a changed row.
    assert slow_job_inventory.queries == 1
    with history_sessions() as session:
        assert session.get(Job, candidate.id) is not None


def test_inventory_budget_restart_recomputes_and_then_progresses(
    history_sessions: sessionmaker[Session], slow_job_inventory: InventoryElapsed
) -> None:
    now = datetime.now(UTC)
    candidate = _job(now - timedelta(days=2))
    with history_sessions.begin() as session:
        session.add(candidate)
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    # Restart discards only observations; repeating the slow successful query
    # must still lead to progress in the replacement collector's next pass.
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    assert slow_job_inventory.queries == 2
    assert collector.collect()["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, candidate.id) is None


def test_a_reference_inventory_slower_than_the_budget_still_advances_the_pass(
    history_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches fixed setup spending the pass budget, so no pass ever progresses."""
    now = datetime.now(UTC)
    candidate = _job(now - timedelta(days=2))
    with history_sessions.begin() as session:
        session.add(candidate)
    elapsed = InventoryElapsed()
    inventory = terminal_history_collection._protected_tokens

    def slow_inventory(session: Session, at: datetime) -> frozenset[str] | None:
        elapsed.seconds += 2.1
        return inventory(session, at)

    monkeypatch.setattr(
        terminal_history_collection,
        "time",
        SimpleNamespace(monotonic=elapsed.monotonic),
    )
    monkeypatch.setattr(
        terminal_history_collection, "_protected_tokens", slow_inventory
    )
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert collector.collect()["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, candidate.id) is None


def test_failed_inventory_does_not_authorize_deletion_or_skip_recovery(
    history_sessions: sessionmaker[Session], slow_job_inventory: InventoryElapsed
) -> None:
    now = datetime.now(UTC)
    candidate = _job(now - timedelta(days=2))
    with history_sessions.begin() as session:
        session.add(candidate)
    engine = history_sessions.kw["bind"]
    assert isinstance(engine, Engine)

    def deny_inventory(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        if statement.startswith("SELECT jobs.id, jobs.updated_at"):
            raise SQLAlchemyError("inventory unavailable")

    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    event.listen(engine, "before_cursor_execute", deny_inventory)
    try:
        assert not collector.tick()
        with history_sessions() as session:
            assert session.get(Job, candidate.id) is not None
    finally:
        event.remove(engine, "before_cursor_execute", deny_inventory)
    assert not collector.collect()
    assert slow_job_inventory.queries == 1
    assert collector.collect()["jobs"] == 1
