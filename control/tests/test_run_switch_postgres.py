"""Run orchestration against the actual fresh PostgreSQL schema and route worker."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select
from vonk_control.models import AgentOperation, RecipeRun
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.run_switch_contract import RunSwitchApplyRequest

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

    @event.listens_for(postgres_engine, "checkout")
    def bounded_locks(connection, _record, _proxy):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '1s'")

    return postgres_engine


def _installed(tmp_path, engine, *, nodes=1):
    sessions, lifecycle, _, mapping, build, node_ids = setup_services(
        tmp_path, engine=engine, create_schema=False, nodes=nodes
    )
    installation = installed_recipe(
        lifecycle, mapping, build, node_ids, request_id=str(uuid.uuid4())
    )
    return sessions, lifecycle, node_ids, installation


def test_postgres_conflicts_deduplicate_distributed_runs_and_preserve_group_safety(
    tmp_path, migrated_engine,
):
    sessions, lifecycle, nodes, installation = _installed(tmp_path, migrated_engine, nodes=2)
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
        conflicts, stops, blockers = service._conflicts(session, nodes[:1], action="switch")
        assert [item.code for item in blockers] == ["run-switch.cross-group_conflict"]
        assert len(conflicts) == 1 and stops == []


def test_postgres_invalid_terminal_child_fails_without_nested_row_lock(tmp_path, migrated_engine):
    sessions, lifecycle, nodes, _ = _installed(tmp_path, migrated_engine)
    artifacts = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions, NOW, lifecycle, artifacts,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    assert service.preview(request, actor="admin").allowed
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())), actor="admin"
    )
    service.tick()
    child_id = service.get(operation.operation_id).result["child_operation_id"]
    artifacts.children[child_id].state = "succeeded"
    artifacts.children[child_id].result = None
    service.tick()
    failed = service.get(operation.operation_id)
    assert failed.state == "failed"
    assert failed.status_reason == "run-switch.transfer-returned-invalid-evidence"
    assert failed.result["retryable"] is False


def _awaiting_final_verification(tmp_path, engine):
    sessions, lifecycle, nodes, _ = _installed(tmp_path, engine)
    publisher = ConcurrentPublisher()
    _, routes = bind_route_publications(sessions, lifecycle, publisher)
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    assert service.preview(request, actor="admin").allowed
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())), actor="admin"
    )
    completed = set()
    for _ in range(20):
        service.tick()
        current = service.get(operation.operation_id)
        assert current.state in {"queued", "running"}, current.status_reason
        child_id = current.result.get("child_operation_id")
        if child_id:
            with sessions() as session:
                children = tuple(session.scalars(select(AgentOperation).where(
                    AgentOperation.parent_job_id == child_id
                )))
            for child in children:
                if child.id not in completed:
                    lifecycle.record_node_result(
                        child_id, child.node_id, succeeded=True,
                        evidence=start_evidence(child.payload),
                    )
                    completed.add(child.id)
        if current.current_phase == "final_verify":
            break
    else:
        pytest.fail("Run did not reach final verification")
    with sessions() as session:
        run = session.scalar(select(RecipeRun))
        assert run.state == "running" and run.route_state == "pending"
        run_id = run.id
    mark_current_exact_observations(sessions, run_id, NOW)
    service.tick()
    waiting = service.get(operation.operation_id)
    assert waiting.state == "running"
    assert waiting.result["final_observation"]["final_verified"] is False
    return sessions, lifecycle, routes, publisher, service, operation


def test_postgres_running_switch_allows_route_publication_and_survives_restart(tmp_path, migrated_engine):
    sessions, lifecycle, routes, publisher, _, operation = _awaiting_final_verification(tmp_path, migrated_engine)
    restarted = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    worker = RecipeOperationWorker(sessions, routes, clock=lambda: NOW, run_switches=restarted)
    for _ in range(4):
        worker.tick()
    result = restarted.get(operation.operation_id)
    assert result.state == "succeeded", result.status_reason
    assert publisher.aliases[-1] == ("qwen",)
    assert any(item.get("final_verified") is True for item in result.result["phase_results"])


def test_postgres_final_verification_has_durable_timeout(tmp_path, migrated_engine):
    sessions, lifecycle, _, _, service, operation = _awaiting_final_verification(tmp_path, migrated_engine)
    before = service.get(operation.operation_id).result
    for _ in range(3):
        service.tick()
    assert service.get(operation.operation_id).result["phase_results"] == before["phase_results"]
    restarted = _service(sessions, NOW + timedelta(seconds=300), lifecycle, RecordingArtifactExecutor())
    restarted.tick()
    failed = restarted.get(operation.operation_id)
    assert failed.state == "failed"
    assert failed.status_reason == "run-switch.final-verification-timeout"
