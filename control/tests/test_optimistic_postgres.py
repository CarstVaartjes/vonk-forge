"""Real MVCC conflicts: a stale plan cannot overwrite a newer Job or its child."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState, UnknownOutcomeError, WaitReason
from vonk_control.jobs import JobService, StaleAttempt
from vonk_control.models import Base, Job, JobAttempt
from vonk_control.optimistic import Applied, Stale, transition


def _queue(engine):
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    jobs = JobService(sessions, clock=lambda: datetime.now(UTC))
    job = jobs.enqueue("probe", "operator", "authority", [], {})
    return sessions, jobs, job


def test_two_decisions_replan_from_committed_winner(postgres_engine) -> None:
    """Catches lost updates, stale-plan replay, and child effects surviving rollback."""
    sessions, jobs, job = _queue(postgres_engine)
    read = threading.Barrier(2)
    winner_committed = threading.Event()
    seen: list[str] = []

    def winner(session: Session) -> None:
        row = session.get(Job, job.id)
        assert row is not None
        read.wait(timeout=30)
        row.state = LifecycleState.CANCELLED

    def loser(session: Session) -> bool:
        row = session.get(Job, job.id)
        assert row is not None
        seen.append(row.state)
        if row.state == LifecycleState.CANCELLED:
            return False
        read.wait(timeout=30)
        assert winner_committed.wait(timeout=30)
        row.state = LifecycleState.SUCCEEDED
        session.add(
            JobAttempt(
                job_id=row.id,
                attempt=1,
                fence="losing-fence",
                worker_id="loser",
                lease_deadline=datetime.now(UTC),
                state=LifecycleState.SUCCEEDED,
            )
        )
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        losing = pool.submit(transition, sessions, loser)
        winning = pool.submit(transition, sessions, winner)
        assert isinstance(winning.result(timeout=10), Applied)
        winner_committed.set()
        outcome = losing.result(timeout=10)
    assert isinstance(outcome, Applied) and outcome.value is False
    assert seen == [LifecycleState.QUEUED, LifecycleState.CANCELLED]
    assert jobs.get(job.id).state == LifecycleState.CANCELLED
    assert jobs.get(job.id).version == job.version + 1
    with sessions() as session:
        assert session.scalar(select(JobAttempt.id)) is None


def test_active_writer_yields_without_waiting_for_its_commit(postgres_engine) -> None:
    """Catches unbounded UPDATE lock waits and repeated attempts on a busy row."""
    sessions, jobs, job = _queue(postgres_engine)
    decisions: list[int] = []

    def decide(session: Session) -> None:
        row = session.get(Job, job.id)
        assert row is not None
        decisions.append(row.version)
        row.state = LifecycleState.SUCCEEDED

    with sessions.begin() as holder:
        row = holder.get(Job, job.id)
        assert row is not None
        row.state = LifecycleState.CANCELLED
        holder.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            # The holder cannot commit until this future finishes. A blocking
            # UPDATE hangs here; the helper must yield while the holder lives.
            outcome = pool.submit(transition, sessions, decide).result(timeout=30)
        assert isinstance(outcome, Stale)
    assert len(decisions) == 2
    assert jobs.get(job.id).state == LifecycleState.CANCELLED
    # A later tick automatically succeeds once the temporary contention clears.
    assert isinstance(transition(sessions, decide), Applied)
    assert jobs.get(job.id).state == LifecycleState.SUCCEEDED


def test_startup_adds_version_without_changing_running_job(postgres_engine) -> None:
    """Catches a nullable/no-default addition or destructive schema adoption."""
    from vonk_control.db.startup import verify_schema_is_current

    _sessions, jobs, job = _queue(postgres_engine)
    fence = jobs.claim("worker", 30, kinds=("probe",))
    assert fence is not None
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN version")
        verify_schema_is_current(connection)
    recovered = jobs.get(job.id)
    assert recovered.version == 1
    assert recovered.state == LifecycleState.RUNNING
    assert recovered.current_attempt == fence.attempt
    jobs.succeed(fence, {})
    assert jobs.get(job.id).state == LifecycleState.SUCCEEDED


def test_completion_revalidates_fence_after_conflict(postgres_engine) -> None:
    """Catches replaying a completion after another worker reclaimed its lease."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    jobs = JobService(sessions, clock=lambda: now)
    job = jobs.enqueue("probe", "operator", "authority", [], {})
    first = jobs.claim("first", 30, kinds=("probe",))
    assert first is not None
    injected = False
    successor = None

    def reclaim(session: Session, _context, _instances) -> None:
        nonlocal now, injected, successor
        if injected or not any(isinstance(row, Job) for row in session.dirty):
            return
        injected = True
        now += timedelta(seconds=31)
        successor = jobs.claim("successor", 30, kinds=("probe",))
        assert successor is not None

    event.listen(sessions.class_, "before_flush", reclaim)
    try:
        with pytest.raises(StaleAttempt):
            jobs.succeed(first, {})
    finally:
        event.remove(sessions.class_, "before_flush", reclaim)
    assert injected and successor is not None
    row = jobs.get(job.id)
    assert row.state == LifecycleState.RUNNING
    assert row.current_attempt == successor.attempt
    with sessions() as session:
        original = session.scalar(
            select(JobAttempt).where(JobAttempt.fence == first.fence)
        )
        assert original is not None
        assert original.state != LifecycleState.SUCCEEDED
    jobs.succeed(successor, {})
    assert jobs.get(job.id).state == LifecycleState.SUCCEEDED


@pytest.mark.parametrize("action", ["heartbeat", "succeed"])
def test_job_service_contention_is_visible_and_recovers(
    postgres_engine, action
) -> None:
    """Catches lock_timeout becoming terminal failure or abandoning child rollback."""
    sessions, jobs, job = _queue(postgres_engine)
    fence = jobs.claim("worker", 30, kinds=("probe",))
    assert fence is not None

    def report() -> None:
        if action == "heartbeat":
            jobs.heartbeat(fence, 30)
        else:
            jobs.succeed(fence, {})

    with sessions.begin() as holder:
        row = holder.get(Job, job.id)
        assert row is not None
        row.version += 1
        holder.flush()
        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            pytest.raises(UnknownOutcomeError) as caught,
        ):
            pool.submit(report).result(timeout=30)
        assert caught.value.typed_reason == WaitReason.SCOPE_CHANGED
    assert jobs.get(job.id).state == LifecycleState.RUNNING
    with sessions() as session:
        attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.fence == fence.fence)
        )
        assert attempt is not None and attempt.state == LifecycleState.RUNNING
    # Recovery needs no reset/revision/new request once the holder commits.
    report()
    if action == "succeed":
        assert jobs.get(job.id).state == LifecycleState.SUCCEEDED


def test_same_clock_heartbeat_revalidates_after_reclaim(postgres_engine) -> None:
    """Catches a child-only lease write skipping the parent's unchanged version."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    jobs = JobService(sessions, clock=lambda: now)
    successor_jobs = JobService(sessions, clock=lambda: now + timedelta(seconds=31))
    job = jobs.enqueue("probe", "operator", "authority", [], {})
    first = jobs.claim("first", 30, kinds=("probe",))
    assert first is not None
    injected = False
    successor = None

    def reclaim(session: Session, _context, _instances) -> None:
        nonlocal injected, successor
        if injected or not session.dirty:
            return
        injected = True
        successor = successor_jobs.claim("successor", 30, kinds=("probe",))
        assert successor is not None

    event.listen(sessions.class_, "before_flush", reclaim)
    try:
        with pytest.raises(StaleAttempt):
            jobs.heartbeat(first, 60)
    finally:
        event.remove(sessions.class_, "before_flush", reclaim)
    assert successor is not None
    assert jobs.get(job.id).current_attempt == successor.attempt
    with sessions() as session:
        old = session.scalar(select(JobAttempt).where(JobAttempt.fence == first.fence))
        assert old is not None and old.lease_deadline == first.lease_deadline


def test_denied_access_is_not_retried_or_softened(postgres_engine) -> None:
    """Catches treating any database refusal as optimistic contention."""
    sessions, _jobs, _job = _queue(postgres_engine)
    calls = []

    def denied(session: Session) -> None:
        calls.append(True)
        session.execute(text("DO $$ BEGIN RAISE insufficient_privilege; END $$"))

    with pytest.raises(ProgrammingError) as caught:
        transition(sessions, denied)
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    assert calls == [True]


def test_returned_result_has_no_open_transaction_or_lock(postgres_engine) -> None:
    """Catches publishing a result while its transaction still holds the Job lock."""
    sessions, jobs, job = _queue(postgres_engine)

    def decide(session: Session) -> None:
        row = session.get(Job, job.id)
        assert row is not None
        row.version += 1

    before = None
    with sessions() as session:
        before = session.scalar(text("SHOW lock_timeout"))
    assert isinstance(transition(sessions, decide), Applied)
    # Simulate the caller doing non-DB work: another executor can commit now.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert isinstance(
            pool.submit(transition, sessions, decide).result(timeout=30), Applied
        )
    with sessions() as session:
        assert session.scalar(text("SHOW lock_timeout")) == before
    assert jobs.get(job.id).version == job.version + 2


def test_guarded_lifecycle_write_cannot_bypass_the_version_fence(
    postgres_engine,
) -> None:
    """Catches bulk state CAS overwriting a same-state but newer operator decision."""
    from vonk_agent_protocol import OperatorActionName
    from vonk_control.lifecycle import CancelRequested
    from vonk_control.lifecycle.job import JobAdapter

    sessions, jobs, job = _queue(postgres_engine)
    now = datetime.now(UTC)
    seen = []

    def decide(session: Session) -> bool:
        row = session.get(Job, job.id)
        assert row is not None
        seen.append(row.operator_decisions)
        if row.operator_decisions is not None:
            return False
        with sessions.begin() as newer:
            winner = newer.get(Job, job.id)
            assert winner is not None
            winner.operator_decisions = OperatorActionName.STOP.value
        return (
            not JobAdapter()
            .settle(session, row, CancelRequested(), now, guard=LifecycleState.QUEUED)
            .stale
        )

    outcome = transition(sessions, decide)
    assert isinstance(outcome, Applied) and outcome.value is False
    assert seen == [None, OperatorActionName.STOP.value]
    assert jobs.get(job.id).state == LifecycleState.QUEUED
