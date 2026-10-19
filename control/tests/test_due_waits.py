"""Quiet PostgreSQL workers recover waits without an ingress side effect."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import timedelta

import pytest
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
        process.wait(timeout=30)
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


def test_logical_owners_sharing_a_table_get_independent_turns(postgres_engine):
    """Catches one Job kind consuming another kind's bounded observation turns."""
    from vonk_agent_protocol import WaitReason
    from vonk_control.operation_blockers import BlockingOperation, make_blocker
    from vonk_control.persisted_waits import write_wait

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    first, second, healthy = _job(), _job(), _job()
    first.id, second.id, healthy.id = sorted((first.id, second.id, healthy.id))
    with sessions.begin() as session:
        for job in (first, second, healthy):
            session.add(job)
            write_wait(
                job,
                identity=BlockingOperation(id=job.id, kind=job.kind),
                kind=WaitOwnerKind.JOB
                if job is not healthy
                else WaitOwnerKind.RUN_SWITCH,
                reason=WaitReason.SPARK_BUSY,
                now=NOW,
                next_check=NOW,
                blockers=[
                    make_blocker(WaitReason.SPARK_BUSY, "accepted child is busy")
                ],
            )
    seen = []
    backstop = DueWaitBackstop(
        sessions,
        [
            WaitSource(Job, frozenset({kind}), lambda key, wait: seen.append(key[0]))
            for kind in (WaitOwnerKind.JOB, WaitOwnerKind.RUN_SWITCH)
        ],
        clock=lambda: NOW,
        batch_limit=1,
    )
    backstop.tick()
    backstop.tick()
    assert seen == [first.id, healthy.id]


def test_runtime_dispatch_covers_every_mapped_wait_owner():
    """Inherited/mixin ORM wait columns cannot evade the static whole-tree guard."""
    from vonk_control.wait_redrive import WAIT_MODELS, wait_sources

    owners = {
        mapper.class_
        for mapper in Base.registry.mappers
        if "wait_evidence" in mapper.columns
    }
    assert owners == set(WAIT_MODELS.values())
    assert set(WaitOwnerKind) == WAIT_MODELS.keys()
    sources = wait_sources(dict.fromkeys(WaitOwnerKind, lambda key, wait: None))
    assert {kind for source in sources for kind in source.kinds} == set(WaitOwnerKind)


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


def test_owner_rotation_recovers_a_terminal_revocation_after_ca_outage(postgres_engine):
    """Catches an always-corrupt owner starving another owner's aliased due clock."""
    from uuid import uuid4

    from vonk_agent_protocol.state_machines import CertificateRecordState
    from vonk_control.enrollment import EnrollmentService
    from vonk_control.models import AgentIssuedCertificateRevocation
    from vonk_control.persisted_waits import read_wait
    from vonk_control.step_ca import StepCAUnavailable

    from .test_agent_jobs_postgres import RevokingAuthority

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    damaged = _job()
    row = AgentIssuedCertificateRevocation(
        serial="due-revocation",
        node_id="spk_" + "a" * 32,
        provider_request_id=str(uuid4()),
        generation=1,
        state=CertificateRecordState.REVOKED,
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions.begin() as session:
        session.add_all((damaged, row))
        schedule_wait(damaged, NOW, now=NOW)
        schedule_wait(row, NOW, now=NOW)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET wait_evidence = '{}' WHERE id = :id"),
            {"id": damaged.id},
        )
    available = [False]
    now = [NOW]
    revoked = []

    class Authority(RevokingAuthority):
        def revoke_node(self, serial, observed_at):
            if not available[0]:
                raise StepCAUnavailable("CA transport unavailable")
            revoked.append(serial)

    def construct():
        service = EnrollmentService(sessions, Authority(), clock=lambda: now[0])
        return DueWaitBackstop(
            sessions,
            [
                WaitSource(
                    Job, frozenset({WaitOwnerKind.JOB}), lambda key, evidence: None
                ),
                WaitSource(
                    AgentIssuedCertificateRevocation,
                    frozenset({WaitOwnerKind.CERTIFICATE_REVOCATION}),
                    lambda key, evidence: service.reconcile_revocations(str(key[0])),
                ),
            ],
            clock=lambda: now[0],
            batch_limit=1,
        )

    first = construct()
    first.tick()
    first.tick()
    assert not revoked
    with sessions() as session:
        pending = session.get(AgentIssuedCertificateRevocation, row.serial)
        assert pending is not None
        wait = read_wait(pending)
        assert wait is not None and wait.next_check > NOW
    available[0] = True
    now[0] = wait.next_check
    restarted = construct()
    restarted.tick()
    restarted.tick()
    assert revoked == [row.serial]
    with sessions() as session:
        settled = session.get(AgentIssuedCertificateRevocation, row.serial)
        assert settled is not None and settled.ca_revoked_at == now[0]
        assert settled.state == CertificateRecordState.REVOKED
        assert read_wait(settled) is None


def test_owner_handler_runs_after_the_selection_connection_is_released(postgres_engine):
    """A one-connection pool catches selection holding SQL across owner I/O."""
    from sqlalchemy import create_engine

    engine = create_engine(
        postgres_engine.url, pool_size=1, max_overflow=0, pool_timeout=30
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    job = _job()
    with sessions.begin() as session:
        session.add(job)
        schedule_wait(job, NOW, now=NOW)
    calls = []

    def handler(request):
        with sessions() as session:
            current = session.get(Job, request.job_id)
            assert current is not None and current.state == LifecycleState.RUNNING
        calls.append(request.job_id)
        return {}

    worker = Worker(
        JobService(sessions, clock=lambda: NOW), "connection-probe", {job.kind: handler}
    )
    backstop = DueWaitBackstop(
        sessions,
        [
            WaitSource(
                Job,
                frozenset({WaitOwnerKind.JOB}),
                lambda key, evidence: worker._run_generic(str(key[0])),
            )
        ],
        clock=lambda: NOW,
    )
    try:
        backstop.tick()
        assert calls == [job.id]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "superseded, missing_receipt", ((False, False), (True, False), (False, True))
)
def test_due_recipe_parent_uses_normal_phase_advancement_after_restart(
    tmp_path, postgres_engine, monkeypatch, request, superseded, missing_receipt
):
    """Catches registering a parent callback that only polls its cleanup paths."""
    from sqlalchemy import select
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.models import AgentOperation
    from vonk_control.presence import ManagementAddressPolicy
    from vonk_control.route_runtime import AtomicRouteBundlePublisher
    from vonk_control.worker import assemble_production_worker

    from .test_recipe_operations import (
        NOW as RECIPE_NOW,
    )
    from .test_recipe_operations import (
        _job_phases,
        installed_recipe,
        setup_services,
        start_evidence,
    )

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=3, engine=postgres_engine
    )
    installed = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="redrive-install"
    )
    plan = service.preview_run(installed.owner_id, "redrive-phases")
    started = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="redrive-start"
    )
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == started.id)
            )
        )
        parent = session.get(Job, started.id)
        assert parent is not None
        expected = {
            item["operation_id"]
            for phase in _job_phases(parent.payload)
            for item in phase
        }
        assert expected > {child.id for child in children}

    # Inject loss of owner advancement after valid node evidence was durably
    # accepted. Its one wait is committed with that unchanged waiting state.
    def interrupted(session, job, *_args, **_kwargs):
        schedule_wait(job, RECIPE_NOW + timedelta(seconds=5), now=RECIPE_NOW)
        return False

    monkeypatch.setattr(service, "_advance_parent_in_session", interrupted)
    for child in children:
        service.record_node_result(
            started.id,
            child.node_id,
            succeeded=True,
            evidence=start_evidence(child.payload),
        )
    saved_result = None
    if missing_receipt:
        from vonk_control.recipe_lifecycle_contract import (
            RecipeOperationProgressResult,
            RecipeOperationResult,
        )
        from vonk_control.stored_json import read_row_column

        with sessions.begin() as session:
            parent = session.get(Job, started.id)
            assert parent is not None
            saved_result = parent.result
            recorded = read_row_column(parent, "result")
            assert isinstance(
                recorded, (RecipeOperationProgressResult, RecipeOperationResult)
            )
            parent.result = recorded.model_copy(
                update={"node_evidence": None, "launch_evidence": None}
            ).model_dump(mode="json", exclude_none=True)
    now = RECIPE_NOW + timedelta(seconds=5)
    if superseded:
        from vonk_agent_protocol import AgentOperation as WireAgentOperation

        with sessions.begin() as session:
            service._admit_workload_intent(
                session,
                kind=WireAgentOperation.RECIPE_START,
                targets=nodes,
                workload_intent_ordinal=None,
                now=now,
            )
    queue = AgentJobService(sessions, clock=lambda: now)
    worker = assemble_production_worker(
        jobs=JobService(sessions, clock=lambda: now),
        sessions=sessions,
        agent_jobs=queue,
        publisher=AtomicRouteBundlePublisher(tmp_path / "routes"),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: now,
        worker_id="phase-backstop",
        artifact_job_root=tmp_path / "artifacts",
        artifact_job_storage_max_bytes=1024**3,
        artifact_job_retention_seconds=86400,
        artifact_job_reconcile_interval_seconds=3600,
        artifact_job_reconcile_batch_limit=16,
    )
    request.addfinalizer(worker.close)
    backstop = worker._due_waits
    assert backstop is not None
    backstop()
    if missing_receipt:
        from vonk_agent_protocol import WaitReason
        from vonk_control.persisted_waits import read_wait

        with sessions.begin() as session:
            parent = session.get(Job, started.id)
            assert parent is not None
            wait = read_wait(parent)
            assert wait is not None and wait.reason is WaitReason.RECEIPT_MISSING
            assert set(
                session.scalars(
                    select(AgentOperation.id).where(
                        AgentOperation.parent_job_id == started.id
                    )
                )
            ) == {child.id for child in children}
            parent.result = saved_result
            now = wait.next_check
        backstop()
    with sessions() as session:
        current = set(
            session.scalars(
                select(AgentOperation.id).where(
                    AgentOperation.parent_job_id == started.id
                )
            )
        )
        if superseded:
            assert current == {child.id for child in children}
            parent = session.get(Job, started.id)
            assert parent is not None and parent.state == LifecycleState.CANCELLED
        else:
            assert current == expected
            assert current > {child.id for child in children}
