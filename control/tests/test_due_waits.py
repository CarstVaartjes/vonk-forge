"""Quiet PostgreSQL workers recover waits without an ingress side effect."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, WaitOwnerKind
from vonk_control.due_waits import DueWaitBackstop, WaitSource
from vonk_control.jobs import JobService
from vonk_control.models import Base, Job
from vonk_control.persisted_waits import schedule_wait
from vonk_control.worker import Worker

from .test_persisted_waits import NOW, _job


def test_quiet_worker_reconstruction_redrives_the_same_fenced_job(postgres_engine):
    """Fails without the backstop: an idle worker never revisits this due owner."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    job = _job()
    with sessions.begin() as session:
        session.add(job)
        schedule_wait(job, NOW + timedelta(seconds=5), now=NOW)
    now = [NOW]
    calls = []

    def construct():
        service = JobService(sessions, clock=lambda: now[0])
        worker = Worker(
            service,
            "quiet-worker",
            {job.kind: lambda request: calls.append(request.job_id) or {}},
        )
        backstop = DueWaitBackstop(
            sessions,
            [
                WaitSource(
                    Job,
                    frozenset({WaitOwnerKind.JOB}),
                    lambda key, wait: worker._run_generic(str(key[0])),
                )
            ],
            clock=lambda: now[0],
        )
        worker._due_waits = backstop.tick
        return backstop

    assert construct().tick() == 0
    # Drop every process-local object, then clear the dependency. No API/agent
    # traffic or ordinary worker claim is used to drive this test.
    now[0] += timedelta(seconds=5)
    assert construct().tick() == 1
    assert calls == [job.id]
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == LifecycleState.SUCCEEDED
        assert stored.current_attempt == 1


def test_corrupt_first_row_cannot_starve_later_due_work(postgres_engine, caplog):
    """Catches a LIMIT-first selector repeatedly returning damaged evidence."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    bad, good = _job(), _job()
    bad.id, good.id = sorted((bad.id, good.id))
    with sessions.begin() as session:
        for job in (bad, good):
            session.add(job)
            schedule_wait(job, NOW, now=NOW)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET wait_evidence = '{}' WHERE id = :id"), {"id": bad.id}
        )
    seen = []
    backstop = DueWaitBackstop(
        sessions,
        [
            WaitSource(
                Job,
                frozenset({WaitOwnerKind.JOB}),
                lambda key, wait: seen.append(key[0]),
            )
        ],
        clock=lambda: NOW,
        batch_limit=1,
    )
    backstop.tick()
    backstop.tick()
    assert seen == [good.id]
    assert bad.id in caplog.text


def test_cancellation_committed_after_selection_wins_before_retry(postgres_engine):
    """Catches retry using a stale selection snapshot instead of the owner claim."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    job = _job()
    with sessions.begin() as session:
        session.add(job)
        schedule_wait(job, NOW, now=NOW)
    calls = []
    worker = Worker(
        JobService(sessions, clock=lambda: NOW),
        "quiet-worker",
        {job.kind: lambda request: calls.append(request.job_id) or {}},
    )

    def advance(key, wait):
        with sessions.begin() as session:
            current = session.get(Job, key[0], with_for_update=True)
            assert current is not None
            current.state = LifecycleState.CANCELLED
            schedule_wait(current, None)
        return worker._run_generic(str(key[0]))

    backstop = DueWaitBackstop(
        sessions,
        [WaitSource(Job, frozenset({WaitOwnerKind.JOB}), advance)],
        clock=lambda: NOW,
    )
    backstop.tick()
    assert not calls
    with sessions() as session:
        current = session.get(Job, job.id)
        assert current is not None and current.current_attempt == 0
