"""Run orchestration against the actual fresh PostgreSQL schema and route worker."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select
from vonk_agent_protocol import LifecycleState, RunSwitchCode
from vonk_agent_protocol.state_machines import RouteState
from vonk_control.logging import configure_controller_logging
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.run_switch_contract import (
    ArtifactVerificationEvidence,
    RunSwitchApplyRequest,
    RunSwitchFinalVerifyResult,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import _stored_job_plan

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    bind_route_publications,
    installed_recipe,
    mark_current_exact_observations,
    setup_services,
    start_evidence,
    started_recipe,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _replace_child,
    _request,
    _service,
)


@pytest.fixture
def migrated_engine(postgres_engine):
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", postgres_engine.url.render_as_string(hide_password=False)
    )
    command.upgrade(config, "head")

    # Production waits 30 s for a row lock. Keep a stuck lock bounded well
    # inside each test's 10 s budget, but long enough that a loaded CI shard
    # does not turn an ordinary serialized wait into LockNotAvailable.
    @event.listens_for(postgres_engine, "checkout")
    def bounded_locks(connection, _record, _proxy):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")

    return postgres_engine


def _installed(tmp_path, engine, *, nodes=1, distributed_lifecycle=False):
    sessions, lifecycle, _, mapping, build, node_ids = setup_services(
        tmp_path,
        engine=engine,
        create_schema=False,
        nodes=nodes,
        distributed_lifecycle=distributed_lifecycle,
    )
    installation = installed_recipe(
        lifecycle, mapping, build, node_ids, request_id=str(uuid.uuid4())
    )
    return sessions, lifecycle, node_ids, installation


def test_postgres_conflicts_deduplicate_distributed_runs_and_preserve_group_safety(
    tmp_path,
    migrated_engine,
):
    sessions, lifecycle, nodes, installation = _installed(
        tmp_path, migrated_engine, nodes=2
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    with sessions() as session:
        assert service._conflicts(session, nodes, action="run") == ([], [], [])
    started_recipe(
        sessions, lifecycle, installation.owner_id, nodes, request_id=str(uuid.uuid4())
    )
    with sessions() as session:
        conflicts, stops, blockers = service._conflicts(session, nodes, action="run")
        assert len(conflicts) == len(stops) == 1
        assert blockers == []
        conflicts, stops, blockers = service._conflicts(
            session, nodes[:1], action="switch"
        )
        assert blockers
        assert len(conflicts) == 1 and stops == []


def test_postgres_invalid_terminal_child_is_retried_without_nested_row_lock(
    tmp_path, migrated_engine
):
    sessions, lifecycle, nodes, _ = _installed(tmp_path, migrated_engine)
    artifacts = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        artifacts,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    assert service.preview(request, actor="admin").allowed
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    service.tick()
    persisted = service.get(operation.operation_id)
    assert persisted.result is not None
    child_id = persisted.result.child_operation_id
    assert child_id is not None
    _replace_child(artifacts, child_id, state="succeeded")
    _replace_child(
        artifacts,
        child_id,
        result=artifacts.children[child_id].result.model_copy(
            update={
                "evidence": [
                    ArtifactVerificationEvidence.model_construct(
                        node_id="", copied_bytes=-1
                    )
                ]
            },
        ),
    )
    service.tick()
    # A receipt that does not validate is an unknown, not a failure: the operation
    # is decided under the one lock it already holds (no nested row lock) and the
    # idempotent child is issued again at the core's backoff.
    held = service.get(operation.operation_id)
    assert held.state == LifecycleState.RUNNING
    assert held.result is not None
    assert held.result.failure_code is None
    assert held.result.retry_reason == "run-switch phase receipt is invalid"
    assert held.result.child_operation_id is None
    assert held.result.observation_due_at is not None


def _awaiting_final_verification(tmp_path, engine, *, distributed=False):
    nodes = 2 if distributed else 1
    sessions, lifecycle, node_ids, _ = _installed(
        tmp_path,
        engine,
        nodes=nodes,
        distributed_lifecycle=distributed,
    )
    publisher = ConcurrentPublisher()
    _, routes = bind_route_publications(sessions, lifecycle, publisher)
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, node_ids[0])
    if distributed:
        request = request.model_copy(
            update={
                "spark_group": SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node_ids[0],
                            rank=0,
                            role="entrypoint",
                            endpoint_owner=True,
                        ),
                        SparkGroupNode(node_id=node_ids[1], rank=1, role="worker"),
                    ]
                )
            }
        )
    assert service.preview(request, actor="admin").allowed
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    completed = set()
    for _ in range(20):
        service.tick()
        current = service.get(operation.operation_id)
        assert current.state in {"queued", "running"}, current.status_reason
        assert current.result is not None
        child_id = current.result.child_operation_id
        if child_id:
            with sessions() as session:
                children = tuple(
                    session.scalars(
                        select(AgentOperation).where(
                            AgentOperation.parent_job_id == child_id
                        )
                    )
                )
            for child in children:
                if child.id not in completed:
                    lifecycle.record_node_result(
                        child_id,
                        child.node_id,
                        succeeded=True,
                        evidence=start_evidence(child.payload),
                    )
                    completed.add(child.id)
        if current.current_phase == "final_verify":
            break
    else:
        pytest.fail("Run did not reach final verification")
    with sessions() as session:
        run = session.scalar(select(RecipeRun))
        assert run is not None
        assert run.state == LifecycleState.RUNNING and run.route_state == "pending"
        run_id = run.id
    mark_current_exact_observations(sessions, run_id, NOW)
    service.tick()
    waiting = service.get(operation.operation_id)
    assert waiting.state == LifecycleState.RUNNING
    assert waiting.result is not None
    assert waiting.result.final_observation is not None
    assert isinstance(waiting.result.final_observation, RunSwitchFinalVerifyResult)
    assert waiting.result.final_observation.final_verified is False
    return sessions, lifecycle, routes, publisher, service, operation


def test_postgres_running_switch_allows_route_publication_and_survives_restart(
    tmp_path, migrated_engine
):
    sessions, lifecycle, routes, publisher, _, operation = _awaiting_final_verification(
        tmp_path, migrated_engine
    )
    now = [NOW]
    restarted = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    restarted._clock = lambda: now[0]
    worker = RecipeOperationWorker(
        sessions, routes, clock=lambda: now[0], run_switches=restarted
    )
    for _ in range(4):
        now[0] += timedelta(seconds=5)
        worker.tick()
    result = restarted.get(operation.operation_id)
    assert result.state == LifecycleState.SUCCEEDED, result.status_reason
    assert publisher.aliases[-1] == ("qwen",)
    assert result.result is not None
    assert any(
        isinstance(item, RunSwitchFinalVerifyResult) and item.final_verified is True
        for item in result.result.phase_results
    )


def test_postgres_final_verification_backs_off_after_durable_deadline(
    tmp_path, migrated_engine
):
    sessions, lifecycle, _, _, service, operation = _awaiting_final_verification(
        tmp_path, migrated_engine
    )
    before_operation = service.get(operation.operation_id)
    assert before_operation.result is not None
    before = before_operation.result
    for _ in range(3):
        service.tick()
    current = service.get(operation.operation_id)
    assert current.result is not None
    assert current.result.phase_results == before.phase_results
    restarted = _service(
        sessions, NOW + timedelta(seconds=300), lifecycle, RecordingArtifactExecutor()
    )
    restarted.tick()
    waiting = restarted.get(operation.operation_id)
    assert waiting.state == LifecycleState.RUNNING
    assert waiting.result is not None
    assert waiting.result.final_verify_started_at == before.final_verify_started_at
    assert waiting.result.observation_due_at == NOW + timedelta(seconds=360)
    assert restarted.tick() is False


def test_postgres_final_verification_waits_after_accepted_start_deadline(
    tmp_path, migrated_engine, capsys
):
    configure_controller_logging()
    sessions, lifecycle, routes, _, service, operation = _awaiting_final_verification(
        tmp_path, migrated_engine, distributed=True
    )
    current = service.get(operation.operation_id)
    assert current.result is not None
    accepted_deadline = current.result.start_deadline
    assert accepted_deadline is not None
    initial_observation = current.result.final_observation
    assert isinstance(initial_observation, RunSwitchFinalVerifyResult)
    run_id = initial_observation.run_id

    now = accepted_deadline + timedelta(seconds=1)
    lifecycle._clock = lambda: now
    service._clock = lambda: now
    assert service.tick() is True
    expiry_log = capsys.readouterr().err
    assert '"event":"run_switch.final_verification_expired"' in expiry_log
    assert run_id in expiry_log

    expired = service.get(operation.operation_id)
    assert expired.state == LifecycleState.OBSERVING
    assert expired.progress.state == LifecycleState.OBSERVING
    assert expired.status_reason is not None
    assert "final-verification-expired" in expired.status_reason
    assert "accepted start deadline" in expired.status_reason
    assert run_id in expired.status_reason
    assert "route-publication-pending" in expired.status_reason
    assert "next observation at" in expired.status_reason
    assert expired.result is not None
    assert expired.result.start_deadline == accepted_deadline
    next_observation_at = expired.result.observation_due_at
    assert next_observation_at is not None
    expired_observation = expired.result.final_observation
    assert isinstance(expired_observation, RunSwitchFinalVerifyResult)
    assert expired_observation.final_verified is False
    assert expired_observation.state == LifecycleState.RUNNING
    assert expired_observation.route_state == "pending"
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == LifecycleState.RUNNING
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert reservations
    assert service.tick() is False
    recovered_at = next_observation_at + timedelta(seconds=1)
    mark_current_exact_observations(sessions, run_id, recovered_at)
    lifecycle._clock = lambda: recovered_at
    routes._clock = lambda: recovered_at
    routes.publish_run(run_id)
    service._clock = lambda: recovered_at
    for _ in range(3):
        service.tick()
    recovered = service.get(operation.operation_id)
    assert recovered.state == LifecycleState.SUCCEEDED, recovered.status_reason
    assert recovered.result is not None
    assert recovered.result.start_deadline == accepted_deadline
    assert any(
        isinstance(item, RunSwitchFinalVerifyResult)
        and item.final_verified is True
        and item.run_id == run_id
        for item in recovered.result.phase_results
    )


def test_postgres_final_verification_timeout_hands_run_to_recovery(
    tmp_path, migrated_engine
):
    sessions, lifecycle, routes, _, service, operation = _awaiting_final_verification(
        tmp_path, migrated_engine, distributed=True
    )
    current = service.get(operation.operation_id)
    assert current.result is not None and current.result.start_deadline is not None
    observation = current.result.final_observation
    assert isinstance(observation, RunSwitchFinalVerifyResult)
    run_id = observation.run_id
    now = max(
        current.result.start_deadline + timedelta(seconds=901),
        NOW + timedelta(seconds=901),
    )
    lifecycle._clock = lambda: now
    service._clock = lambda: now

    assert service.tick() is True
    failed = service.get(operation.operation_id)
    assert failed.state == LifecycleState.FAILED
    assert failed.status_reason is not None
    assert failed.result is not None
    assert failed.result.failure_code == RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
    assert failed.status_reason.startswith(RunSwitchCode.FINAL_VERIFICATION_TIMEOUT)
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        nodes = tuple(session.scalars(select(RunNode).where(RunNode.run_id == run_id)))
        assert run is not None and run.state == LifecycleState.RUNNING
        # The fixture has never published a working route. Recovery retains
        # the run and schedules its route worker rather than claiming a stop.
        assert observation.route_state == RouteState.PENDING
        assert run.route_state == RouteState.PENDING
        assert nodes and all(node.state == LifecycleState.RUNNING for node in nodes)
    # A settled observation timeout must release the planner's busy state even
    # while the existing run's route worker owns exact workload recovery.
    with sessions() as session:
        job = session.get(Job, failed.operation_id)
        assert job is not None
        plan = _stored_job_plan(job)
        assert plan is not None
    request = _request(sessions, failed.node_ids[0]).model_copy(
        update={"spark_group": plan.spark_group}
    )
    fresh = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert fresh.operation_id != failed.operation_id
    # The fresh request adopts the exact running workload and observes its
    # route, rather than queuing another launch or refusing the old outcome.
    assert fresh.state == LifecycleState.OBSERVING
    assert fresh.node_ids == failed.node_ids
    # Exact observations recover through the normal route worker; a terminal
    # planner row must not poison the still-running workload's publication.
    mark_current_exact_observations(sessions, run_id, now)
    routes._clock = lambda: now
    routes.publish_run(run_id)
    with sessions() as session:
        recovered = session.get(RecipeRun, run_id)
        assert recovered is not None and recovered.state == LifecycleState.RUNNING
        assert recovered.route_state == RouteState.PUBLISHED


def test_postgres_newer_intent_supersedes_parked_final_verification(
    tmp_path, migrated_engine
):
    sessions, lifecycle, _, _, service, operation = _awaiting_final_verification(
        tmp_path, migrated_engine, distributed=True
    )
    current = service.get(operation.operation_id)
    assert current.result is not None
    accepted_deadline = current.result.start_deadline
    assert accepted_deadline is not None
    now = accepted_deadline + timedelta(seconds=1)
    lifecycle._clock = lambda: now
    service._clock = lambda: now
    assert service.tick() is True

    expired = service.get(operation.operation_id)
    assert expired.state == LifecycleState.OBSERVING
    assert expired.result is not None
    next_observation_at = expired.result.observation_due_at
    assert next_observation_at is not None

    with sessions.begin() as session:
        for node_id in expired.node_ids:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal += 1

    lifecycle._clock = lambda: next_observation_at + timedelta(seconds=1)
    service._clock = lambda: next_observation_at + timedelta(seconds=1)
    assert service.tick() is True
    superseded = service.get(operation.operation_id)
    assert superseded.state == LifecycleState.CANCELLED
    assert superseded.status_reason is not None
    assert "superseded" in superseded.status_reason


def test_postgres_duplicate_apply_converges_under_target_lock(
    tmp_path, migrated_engine
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    sessions, lifecycle, nodes, _ = _installed(tmp_path, migrated_engine)
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    key = str(uuid.uuid4())
    barrier = Barrier(2)

    def apply():
        barrier.wait(timeout=5)
        return service._apply_plan(
            plan,
            request_key=key,
            actor="admin",
            kind="recipe.run-switch.v2",
            workload_intent_ordinal=None,
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(apply) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0].operation_id == results[1].operation_id
