from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_control.distributed_lifecycle import DistributedLifecycleError
from vonk_control.distributed_recovery import (
    DistributedRecoveryCoordinator,
    enforce_recovery_deadline,
)
from vonk_control.models import AgentOperation, Job, RecipeRun, RunNode

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    _queued_distributed_recovery_stop,
    _recovery_deadline,
    _typed_stop_payloads,
    bind_route_publications,
    complete_collective_readiness,
    installed_recipe,
    mark_current_exact_observations,
    record_exact_empty_snapshot,
    setup_services,
    start_evidence,
    started_recipe,
)


def test_slow_distributed_restart_retains_accepted_startup_budget(
    tmp_path, postgres_engine
):
    sessions, service, routes, _publisher, started, stop, nodes = (
        _queued_distributed_recovery_stop(
            tmp_path, engine=postgres_engine, startup_budget=300
        )
    )
    stop_finished_at = NOW + timedelta(seconds=45)
    service._clock = lambda: stop_finished_at
    stop_payloads = _typed_stop_payloads(stop.payload)
    assert len(stop_payloads) == len(nodes)
    assert {payload.run_id for payload in stop_payloads} == {started.owner_id}
    assert {payload.target_runtime_id for payload in stop_payloads} == {
        started.owner_id
    }
    assert {payload.plan_digest for payload in stop_payloads} == {started.plan_digest}
    assert {payload.run_generation for payload in stop_payloads} == {1}
    assert all(payload.cancel_pending_start for payload in stop_payloads)
    with sessions() as session:
        advanced_run = session.get(RecipeRun, started.owner_id)
        source_starts = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == started.id)
            )
        )
        assert advanced_run is not None and advanced_run.run_generation == 2
        source_generations = {
            operation.node_id: operation.payload["run_generation"]
            for operation in source_starts
        }
        assert source_generations == {node_id: 1 for node_id in nodes}
    assert {
        payload.node_id: payload.run_generation for payload in stop_payloads
    } == source_generations
    for node_id in nodes:
        service.record_node_result(
            stop.id, node_id, succeeded=True, evidence={"stopped": True}
        )
    with sessions() as session:
        restart = session.scalar(
            select(Job).where(
                Job.kind == "recipe.start",
                Job.payload["owner_id"].as_string() == started.owner_id,
                Job.id != started.id,
            )
        )
        assert restart is not None
    # A real model reload can take four minutes, longer than the ordinary
    # readiness probe or a stop timeout, while its accepted startup budget fits.
    ready_at = NOW + timedelta(seconds=240)
    service._clock = lambda: ready_at
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == restart.id)
            )
        )
    for launch in launches:
        service.record_node_result(
            restart.id,
            launch.node_id,
            succeeded=True,
            evidence=start_evidence(launch.payload),
        )
    complete_collective_readiness(sessions, service, restart.id, nodes[0])
    mark_current_exact_observations(sessions, started.owner_id, ready_at)
    routes._clock = lambda: ready_at
    routes.publish_run(started.owner_id)
    with sessions() as session:
        stored = session.get(Job, restart.id)
        run = session.get(RecipeRun, started.owner_id)
        assert stored is not None and run is not None
        assert stored.state == "succeeded"
        assert run.route_state == "published"
        deadline = _recovery_deadline(stored)
        assert deadline == NOW + timedelta(seconds=300 + 2 * 30)
        assert deadline == _recovery_deadline(stop)
        assert stored.payload["start_deadline"] == deadline.isoformat()
        assert all(
            launch.payload["start_deadline"] == deadline.isoformat()
            for launch in launches
        )
        assert enforce_recovery_deadline(
            stored.payload, now=deadline - timedelta(microseconds=1)
        )
        with pytest.raises(DistributedLifecycleError, match="deadline elapsed"):
            enforce_recovery_deadline(stored.payload, now=deadline)


def test_singleton_recovery_cooldown_is_durable_and_resumes_after_expiry(
    tmp_path,
):
    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=1, distributed_lifecycle=True
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    now = [NOW]
    with sessions.begin() as session:
        run = session.get(RecipeRun, started.owner_id)
        node = session.scalar(select(RunNode).where(RunNode.run_id == started.owner_id))
        assert run is not None and node is not None
        run.recovery_attempts = 5
        run.route_next_attempt_at = None
        node.state = "failed"
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: now[0]
    )

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.recovery_attempts == 6
        assert run.route_next_attempt_at == NOW + timedelta(minutes=5)
        assert "degraded" in (run.route_error or "")
    now[0] += timedelta(minutes=5)

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.recovery_attempts == 0
        assert run.route_next_attempt_at == now[0] + timedelta(seconds=5)
        assert "fresh exact signed absence" in (run.route_error or "")


def test_one_shot_recovery_reports_why_replay_is_unsafe(tmp_path):
    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=1, distributed_lifecycle=True
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="j" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="s" * 36
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    with sessions.begin() as session:
        run = session.get(RecipeRun, started.owner_id)
        node = session.scalar(select(RunNode).where(RunNode.run_id == started.owner_id))
        assert run is not None and node is not None
        plan = dict(run.plan)
        plan["execution_mode"] = "one-shot-jobs"
        run.plan = plan
        node.state = "failed"
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.state == "failed"
        assert "one-shot job may have completed external effects" in (
            run.route_error or ""
        )
        assert "submit a new authorized run" in (run.route_error or "")


@pytest.mark.parametrize(
    "invalid", ["missing", "missing-job", "malformed", "unbounded", "wrong-plan"]
)
def test_recovery_refuses_missing_or_invalid_original_start_authority(
    tmp_path, invalid
):
    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    with sessions.begin() as session:
        job = session.get(Job, started.id)
        assert job is not None
        payload = dict(job.payload)
        if invalid == "missing":
            payload.pop("start_deadline")
        elif invalid == "missing-job":
            payload["owner_id"] = "missing"
        elif invalid == "malformed":
            payload["start_deadline"] = "tomorrow"
        elif invalid == "unbounded":
            payload["start_deadline"] = (NOW + timedelta(days=1)).isoformat()
        else:
            payload["plan_digest"] = "0" * 64
        job.payload = payload
    record_exact_empty_snapshot(sessions, nodes[1], NOW + timedelta(seconds=1))
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )
    assert recovery.tick()
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        assert run.state == "failed"
        assert run.route_state == "withdrawn"
        assert run.route_error is not None and "start authority" in run.route_error
        assert not tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
