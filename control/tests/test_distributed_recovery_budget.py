from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_control.distributed_lifecycle import DistributedLifecycleError
from vonk_control.distributed_recovery import (
    DistributedRecoveryCoordinator,
    enforce_recovery_deadline,
)
from vonk_control.models import AgentOperation, Job, RecipeRun

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    _queued_distributed_recovery_stop,
    _recovery_deadline,
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
