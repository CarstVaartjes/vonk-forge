"""Wait custody across the real owner -> PostgreSQL -> API projection seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, WaitReason
from vonk_control.lifecycle.job import JobAdapter
from vonk_control.models import Base, Job

from tests.signed_recipe_release import signed_recipe_releases

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def _job() -> Job:
    return JobAdapter.new_job(
        id=str(uuid4()),
        request_id=str(uuid4()),
        kind="wait-probe",
        actor="test",
        authority_revision="test",
        targets=[],
        payload_digest="0" * 64,
        payload={},
        result={"flag": False, "count": 0, "value": None},
        current_attempt=0,
        created_at=NOW,
        updated_at=NOW,
    )


def test_generic_adapter_persists_its_wait_clock_and_cause() -> None:
    """Fails on main: JobAdapter.apply discards the lifecycle's due clock."""
    adapter = JobAdapter()
    job = _job()
    before = adapter.adopt(job)
    due = NOW + timedelta(seconds=30)
    after = replace(
        before,
        state=LifecycleState.BACKOFF,
        next_action_at=due,
        reason=WaitReason.SPARK_BUSY,
    )
    adapter.apply(job, after, NOW)
    assert getattr(job, "next_action_at", None) == due
    from vonk_control.persisted_waits import read_wait

    wait = read_wait(job)
    assert wait is not None and wait.reason is WaitReason.SPARK_BUSY
    assert wait.next_check == due


def test_wait_round_trip_reconstruction_projection_and_rollback(
    postgres_engine,
) -> None:
    """Catches dropped enum/nullable evidence, split transactions and read churn."""
    from vonk_agent_protocol import WaitOwnerKind, WaitResumeCondition
    from vonk_control.operation_api.lane_wait import apply_queue_wait
    from vonk_control.operation_blockers import (
        BlockingOperation,
        OperationBlocker,
        StorageReliefEvidence,
    )
    from vonk_control.operation_item_contract import OperationItem
    from vonk_control.persisted_waits import read_wait, schedule_wait, write_wait

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    job = _job()
    due = NOW + timedelta(seconds=20)
    blocker = OperationBlocker(
        code=WaitReason.SPARK_BUSY,
        detail="Exact child still owns the Spark",
        severity="info",
        operation=BlockingOperation(id="child-id", kind="child"),
        storage=StorageReliefEvidence(
            required_free_bytes=0, available_bytes=0, reclaimable_bytes=0
        ),
    )
    with sessions.begin() as session:
        session.add(job)
        write_wait(
            job,
            identity=BlockingOperation(id=job.id, kind=job.kind),
            kind=WaitOwnerKind.JOB,
            reason=WaitReason.SPARK_BUSY,
            now=NOW,
            next_check=due,
            blockers=[blocker],
            dependency_deadline=NOW + timedelta(minutes=5),
            resume_condition=WaitResumeCondition.DEPENDENCY_CHANGED,
        )
    # New session is the same store/load boundary a reconstructed worker uses.
    with sessions() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        wait = read_wait(loaded)
        assert wait is not None and wait.blockers == [blocker]
        assert wait.next_check == due and wait.reason is WaitReason.SPARK_BUSY
        raw = session.execute(
            text("SELECT wait_evidence FROM jobs WHERE id = :id"), {"id": job.id}
        ).scalar_one()
        assert "next_check" not in raw
        assert "dependency_deadline" in raw
        assert "last_progress_at" in raw and raw["last_progress_at"] is None
        assert loaded.result == {"flag": False, "count": 0, "value": None}
        from vonk_control.run_switch_journal_repair.discovery import _fingerprint

        fingerprint = _fingerprint(loaded)
        session.expire(loaded)
        assert _fingerprint(loaded) == fingerprint
        item = OperationItem(
            id=job.id,
            node_ids=[],
            kind=job.kind,
            state=LifecycleState.QUEUED,
            attempt=0,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            supported_actions=[],
        )
        projected = apply_queue_wait(item, wait)
        assert projected.wait == wait and projected.next_attempt_at == due.isoformat()
        assert projected.wait is not None
        storage = projected.wait.blockers[0].storage
        assert storage is not None and storage.available_bytes == 0
        assert read_wait(loaded) == wait  # observation cannot change custody
    with pytest.raises(RuntimeError), sessions.begin() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        schedule_wait(loaded, due + timedelta(seconds=10))
        loaded.state = LifecycleState.RUNNING
        raise RuntimeError("injected abort before commit")
    with sessions.begin() as session:
        loaded = session.scalar(select(Job).where(Job.id == job.id))
        assert loaded is not None and loaded.state == LifecycleState.QUEUED
        assert loaded.next_action_at == due
        previous = read_wait(loaded)
        schedule_wait(loaded, due + timedelta(seconds=30), now=due)
        later = read_wait(loaded)
        assert later is not None and previous is not None
        assert later.first_blocked_at == previous.first_blocked_at
        assert later.last_progress_at == previous.last_progress_at
        assert later.dependency_deadline == previous.dependency_deadline
        # A newly observed cause must replace the lead blocker without losing
        # this accepted owner's hard reconciliation bound.
        schedule_wait(
            loaded,
            due + timedelta(seconds=40),
            now=due,
            reason=WaitReason.RECEIPT_MISSING,
        )
    with sessions() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        changed = read_wait(loaded)
        assert changed is not None
        assert changed.reason is changed.blockers[0].code is WaitReason.RECEIPT_MISSING
        assert changed.dependency_deadline == NOW + timedelta(minutes=5)
        assert changed.last_progress_at is None
    with sessions.begin() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        schedule_wait(loaded, due, deadline=NOW + timedelta(minutes=4))
        schedule_wait(loaded, due, deadline=NOW + timedelta(minutes=9))
    with sessions() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        bounded = read_wait(loaded)
        assert bounded is not None
        assert bounded.dependency_deadline == NOW + timedelta(minutes=4)


def test_startup_adds_wait_columns_without_restarting_a_live_attempt(
    postgres_engine,
) -> None:
    """Catches an upgrade discarding running work or treating NULL waits as failure."""
    from sqlalchemy import inspect
    from vonk_control.db import reconcile_schema
    from vonk_control.models import JobAttempt

    Base.metadata.create_all(postgres_engine)
    identifier, request_id, fence = str(uuid4()), str(uuid4()), str(uuid4())
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN wait_evidence")
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN next_action_at")
        connection.execute(
            text("""INSERT INTO jobs
            (id, request_id, kind, state, actor, authority_revision, targets,
             payload_digest, payload, current_attempt, created_at, updated_at)
            VALUES (:id, :request, 'wait-probe', 'running', 'test', 'test',
                    '[]', :digest, '{}', 1, :now, :now)"""),
            {"id": identifier, "request": request_id, "digest": "0" * 64, "now": NOW},
        )
        connection.execute(
            text("""INSERT INTO job_attempts
            (id, job_id, attempt, fence, worker_id, lease_deadline, state)
            VALUES (:id, :job, 1, :fence, 'executor', :lease, 'running')"""),
            {
                "id": str(uuid4()),
                "job": identifier,
                "fence": fence,
                "lease": NOW + timedelta(minutes=5),
            },
        )
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
    sessions = sessionmaker(postgres_engine)
    with sessions() as session:
        job = session.get(Job, identifier)
        attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == identifier)
        )
        assert job is not None and attempt is not None
        adopted = JobAdapter().lifecycle(job, attempt)
        assert adopted.state is LifecycleState.RUNNING
        assert adopted.fence == fence and adopted.attempt == 1
        assert adopted.lease_deadline == NOW + timedelta(minutes=5)
        assert job.wait_evidence is None
    assert {
        column["name"] for column in inspect(postgres_engine).get_columns("jobs")
    } >= {"wait_evidence", "next_action_at"}


def test_wait_model_inventory_is_exhaustive() -> None:
    """Every concrete owner binds its JSON to the canonical persisted model."""
    from vonk_control.models import Base
    from vonk_control.stored_json import binding_for
    from vonk_control.wait_contract import WaitEvidence

    owners = [
        table
        for table in Base.metadata.tables.values()
        if "wait_evidence" in table.columns
    ]
    assert owners
    for table in owners:
        binding = binding_for(table.name, "wait_evidence")
        assert binding.contract_for(None) is WaitEvidence
        due = [
            column
            for column in table.columns
            if column.name
            in {
                "next_action_at",
                "route_next_attempt_at",
                "ca_next_attempt_at",
                "next_attempt_at",
                "reconciliation_next_at",
            }
        ]
        assert len(due) == 1, table.name


def test_every_durable_wait_owner_has_identity_and_projection() -> None:
    """Catches an owner added to storage without custody or an API observation."""
    from datetime import UTC, datetime

    from sqlalchemy import Integer
    from vonk_agent_protocol import WaitOwnerKind, WaitReason, WaitResumeCondition
    from vonk_control.models import Base, Job
    from vonk_control.operation_blockers import make_blocker
    from vonk_control.persisted_waits import owner_identity, read_wait, write_wait

    now = datetime(2026, 10, 19, tzinfo=UTC)
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if not hasattr(model, "wait_evidence"):
            continue
        keys = {
            str(column.key): 1 if isinstance(column.type, Integer) else "owner-id"
            for column in mapper.primary_key
        }
        if model is Job:
            keys["kind"] = "probe"
        owner = model(**keys)
        identity, kind = owner_identity(owner)
        assert isinstance(kind, WaitOwnerKind)
        write_wait(
            owner,
            identity=identity,
            kind=kind,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            now=now,
            next_check=now,
            blockers=[
                make_blocker(
                    WaitReason.OBSERVATION_UNAVAILABLE,
                    "Dependency observation unavailable",
                )
            ],
            resume_condition=WaitResumeCondition.EFFECT_OBSERVED,
        )
        projected = read_wait(owner)
        assert projected is not None and projected.owner == identity


def test_corrupt_wait_does_not_erase_intent_or_hide_another_owner(
    postgres_engine,
) -> None:
    """Catches one malformed historical row stopping projection of eligible work."""
    from vonk_control.lifecycle.evidence import Residue
    from vonk_control.persisted_waits import read_wait

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    adapter = JobAdapter()
    damaged, healthy = _job(), _job()
    due = NOW + timedelta(seconds=10)
    with sessions.begin() as session:
        for job in (damaged, healthy):
            adapter.apply(
                job,
                replace(
                    adapter.adopt(job),
                    state=LifecycleState.BACKOFF,
                    next_action_at=due,
                    reason=WaitReason.SPARK_BUSY,
                ),
                NOW,
            )
            session.add(job)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET wait_evidence = '{}' WHERE id = :id"),
            {"id": damaged.id},
        )
    with sessions() as session:
        bad = session.get(Job, damaged.id)
        good = session.get(Job, healthy.id)
        assert bad is not None and good is not None
        assert isinstance(bad.wait_evidence, Residue) and read_wait(bad) is None
        assert adapter.adopt(bad).id == damaged.id
        assert (
            bad.request_id == damaged.request_id and bad.state == LifecycleState.QUEUED
        )
        projected = read_wait(good)
        assert projected is not None and projected.next_check == due


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_catalog_wait_survives_reconstruction_and_reuses_exact_observation(
    tmp_path, postgres_engine
) -> None:
    """Catches a restarted loop losing its delay or attaching it to unrelated history."""
    from vonk_agent_protocol import CatalogSyncCode
    from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
    from vonk_control.models import RecipeLibrarySyncRun
    from vonk_control.persisted_waits import read_wait

    from .test_catalog_sync import _fixture

    sessions, catalog, reader, _item = _fixture(tmp_path, engine=postgres_engine)
    now = [NOW]

    def service():
        return ManagedRecipeCatalogSyncService(
            sessions, catalog=catalog, reader=reader, clock=lambda: now[0]
        )

    first = service().automatic()
    service().record_automatic_wait(30, failed=False, observed_id=first.id)
    assert service().automatic_wait_delay() == 30
    with sessions() as session:
        stored = session.get(RecipeLibrarySyncRun, first.id)
        assert stored is not None
        before = read_wait(stored)
        assert before is not None
        assert before.reason is CatalogSyncCode.REFRESH_SCHEDULED
        assert before.owner.id == first.id
    now[0] += timedelta(seconds=30)
    # Unchanged content reuses the original completed observation, not a new run.
    second = service().automatic()
    assert second.id == first.id
    service().record_automatic_wait(30, failed=False, observed_id=second.id)
    latest = service().latest()
    assert latest is not None and latest.wait is not None
    assert latest.wait.next_check == now[0] + timedelta(seconds=30)
    assert latest.wait.first_blocked_at == before.first_blocked_at
    assert latest.wait.last_progress_at is None


def test_stall_alarm_is_not_a_new_dependency_or_progress() -> None:
    """Catches an attention warning resetting the dependency's original age."""
    from vonk_agent_protocol import ProgressAlarmCode
    from vonk_control.operation_blockers import make_blocker
    from vonk_control.persisted_waits import read_wait, schedule_wait

    job = _job()
    cause = make_blocker(WaitReason.SPARK_BUSY, "exact child still owns the Spark")
    schedule_wait(job, NOW, now=NOW, reason=WaitReason.SPARK_BUSY, blockers=[cause])
    alarm = make_blocker(ProgressAlarmCode.STALLED, "No measured child progress")
    later = NOW + timedelta(minutes=20)
    schedule_wait(
        job, later, now=later, reason=WaitReason.SPARK_BUSY, blockers=[cause, alarm]
    )
    wait = read_wait(job)
    assert wait is not None
    assert wait.first_blocked_at == NOW and wait.last_progress_at is None


def test_operator_wait_retains_the_owners_real_action_after_store_load(
    postgres_engine,
) -> None:
    """Catches publishing an operator wait without the resume action its owner accepts."""
    from vonk_agent_protocol import OperatorActionName, WaitResumeCondition
    from vonk_control.persisted_waits import read_wait, schedule_wait

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    adapter, job = JobAdapter(), _job()
    after = replace(
        adapter.adopt(job),
        state=LifecycleState.NEEDS_OPERATOR,
        reason=WaitReason.JOB_STATE_UNCERTAIN,
    )
    with sessions.begin() as session:
        session.add(job)
        adapter.apply(job, after, NOW)
    with sessions.begin() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        wait = read_wait(loaded)
        assert wait is not None
        assert wait.resume_condition is WaitResumeCondition.OPERATOR_ACTION
        assert wait.operator_action is OperatorActionName.RESUME
        assert wait.operator_action in adapter.actions(adapter.adopt(loaded))
        schedule_wait(loaded, NOW + timedelta(seconds=30))
    with sessions() as session:
        loaded = session.get(Job, job.id)
        assert loaded is not None
        wait = read_wait(loaded)
        assert wait is not None and wait.operator_action is OperatorActionName.RESUME
