"""Quiet PostgreSQL workers recover waits without an ingress side effect."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
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
    script = """
import os, signal
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.jobs import JobService
from vonk_control.models import Job
from vonk_control.persisted_waits import schedule_wait
from vonk_control.worker import Worker
sessions = sessionmaker(create_engine(os.environ['WAIT_TEST_DATABASE']))
now = datetime.fromisoformat(os.environ['WAIT_TEST_NOW'])
worker = Worker(JobService(sessions, clock=lambda: now), 'doomed-worker', {})
with sessions.begin() as session:
    job = session.get(Job, os.environ['WAIT_TEST_JOB'])
    schedule_wait(job, datetime.fromisoformat(os.environ['WAIT_TEST_DUE']), now=now)
print('wait committed', flush=True)
signal.pause()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "WAIT_TEST_DATABASE": postgres_engine.url.render_as_string(
                hide_password=False
            ),
            "WAIT_TEST_JOB": job.id,
            "WAIT_TEST_NOW": NOW.isoformat(),
            "WAIT_TEST_DUE": (NOW + timedelta(seconds=5)).isoformat(),
        },
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "wait committed"
    finally:
        process.kill()
        process.wait(timeout=5)
    now = [NOW]
    calls = []

    def construct():
        service = JobService(sessions, clock=lambda: now[0])
        worker = Worker(
            service,
            "quiet-worker",
            {job.kind: lambda request: calls.append(request.job_id) or {}},
            # Saturate the ordinary sources: only the independent backstop
            # may touch the persisted request in this test.
            background_services=(lambda: True,),
        )
        backstop = DueWaitBackstop(
            sessions,
            [
                WaitSource(
                    Job,
                    frozenset({WaitOwnerKind.JOB}),
                    lambda key, wait: worker._run_generic(),
                )
            ],
            clock=lambda: now[0],
        )
        worker._due_waits = backstop.tick
        return worker

    construct().run_once()
    assert not calls
    # Drop every process-local object, then clear the dependency. No API/agent
    # traffic or ordinary worker claim is used to drive this test.
    now[0] += timedelta(seconds=5)
    construct().run_once()
    assert calls == [job.id]
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == LifecycleState.SUCCEEDED
        assert stored.current_attempt == 1


def test_corrupt_first_row_cannot_starve_later_due_work(postgres_engine, caplog):
    """Catches a LIMIT-first selector repeatedly returning damaged evidence."""
    caplog.set_level(logging.INFO, logger="vonk_control.due_waits")
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


def test_terminal_retirement_is_redriven_without_reopening_its_result(
    tmp_path, postgres_engine
):
    """Catches terminal filtering and a lost cleanup handoff after an outage."""
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.persisted_waits import read_wait
    from vonk_control.recipe_operations import RecipeOperationService

    from .test_retirement_recovery import _parked_start

    sessions, lifecycle, _jobs, projection, started, _claim = _parked_start(
        tmp_path, postgres_engine
    )
    assert projection.retire_job is not None
    projection.retire_job(started.id)
    with sessions() as session:
        retired = session.get(Job, started.id)
        assert retired is not None
        result, state = retired.result, retired.state
        assert state in (LifecycleState.FAILED, LifecycleState.CANCELLED)
        wait = read_wait(retired)
        assert wait is not None and wait.owner_kind is WaitOwnerKind.RECIPE_OPERATION
    now = [wait.next_check]
    dependency = [False]

    def withdrawal(run_id):
        if not dependency[0]:
            raise OSError("route publisher unavailable")

    def construct():
        jobs = AgentJobService(sessions, clock=lambda: now[0])
        owner = RecipeOperationService(
            sessions,
            install_admission=lifecycle._install_admission,
            run_admission=lifecycle._run_admission,
            agent_jobs=jobs,
            clock=lambda: now[0],
        )
        owner._route_withdrawer = withdrawal
        return DueWaitBackstop(
            sessions,
            [
                WaitSource(
                    Job,
                    frozenset({WaitOwnerKind.RECIPE_OPERATION}),
                    lambda key, evidence: owner.redrive(str(key[0])),
                )
            ],
            clock=lambda: now[0],
        )

    construct().tick()
    # Reconstruct after the fault clears. No claim, heartbeat, API read or
    # operator re-submission makes the accepted Stop progress.
    dependency[0] = True
    now[0] += timedelta(seconds=6)
    construct().tick()
    from sqlalchemy import select
    from vonk_agent_protocol import AgentOperation

    with sessions() as session:
        stops = list(
            session.scalars(select(Job).where(Job.kind == AgentOperation.RECIPE_STOP))
        )
        assert stops and any(stop.payload.get("phases") for stop in stops)
        retired = session.get(Job, started.id)
        assert (
            retired is not None and retired.state == state and retired.result == result
        )
