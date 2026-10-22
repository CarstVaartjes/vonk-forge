from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS
from vonk_control.distributed_recovery import (
    DistributedRecoveryCoordinator,
    _recovery_marker,
    enforce_recovery_deadline,
)
from vonk_control.models import AgentNode, AgentOperation, Job, RecipeRun, RunNode

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


@pytest.mark.usefixtures("damaged_json_rows")
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
    assert [payload.run_generation for payload in stop_payloads] == [1] * len(nodes)
    for node_id in nodes:
        service.record_node_result(stop.id, node_id, succeeded=True, evidence={})
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
        with pytest.raises(Exception) as _ending:
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
        run.next_action_at = None
        node.state = "failed"
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: now[0]
    )

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        # Spent issued recoveries pause for the cooldown; the next window is fresh.
        assert run is not None and run.recovery_attempts == 0
        assert run.next_action_at is not None
        assert run.next_action_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(
            minutes=5
        )
        assert "degraded" in (run.route_error or "")
    now[0] += timedelta(minutes=5)

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        # Waiting for fresh absence evidence is a wait: it spends no attempt.
        assert run is not None and run.recovery_attempts == 0
        assert run.next_action_at is not None
        due = run.next_action_at.replace(tzinfo=NOW.tzinfo)
        assert now[0] < due <= now[0] + timedelta(seconds=15)
        assert "fresh exact absence" in (run.route_error or "")


def test_distributed_recovery_waits_for_fresh_presence_then_resumes(
    tmp_path,
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
    record_exact_empty_snapshot(sessions, nodes[1], NOW + timedelta(seconds=1))
    stale_at = NOW - timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS + 1)
    with sessions.begin() as session:
        for presence in session.scalars(select(AgentNode)):
            presence.management_observed_at = stale_at
    now = [NOW]
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: now[0]
    )

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        # Stale presence evidence must wait instead of entering Start payloads.
        assert run.state == "running"
        assert "fresh Controller-observed Spark presence" in (run.route_error or "")
        assert run.next_action_at is not None
        assert not tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
        # The wait must not outlive the recovery generation bump: the next
        # attempt still stops the exact Start payloads of generation 1.
        assert run.run_generation == 1
        assert run.plan["run_generation"] == 1

    with sessions.begin() as session:
        for presence in session.scalars(select(AgentNode)):
            presence.management_observed_at = now[0]
    # The pending wait scheduled its next check; recovery resumes once due.
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.next_action_at is not None
        now[0] = run.next_action_at.replace(tzinfo=NOW.tzinfo)

    assert recovery.tick() is True
    with sessions() as session:
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == started.owner_id,
            )
        )
        run = session.get(RecipeRun, started.owner_id)
        assert stop is not None and run is not None
        assert run.run_generation == 2


def test_distributed_recovery_backoff_survives_restart_and_resumes(
    tmp_path,
    postgres_engine,
):
    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True, engine=postgres_engine
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    record_exact_empty_snapshot(sessions, nodes[1], NOW + timedelta(seconds=1))
    stale_at = NOW - timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS + 1)
    with sessions.begin() as session:
        for presence in session.scalars(select(AgentNode)):
            presence.management_observed_at = stale_at
    now = [NOW]
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: now[0]
    )

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        # Stale presence evidence must wait instead of entering Start payloads.
        assert run.state == "running"
        assert "fresh Controller-observed Spark presence" in (run.route_error or "")
        assert run.next_action_at is not None
        assert not tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
        # The wait must not outlive the recovery generation bump: the next
        # attempt still stops the exact Start payloads of generation 1.
        assert run.run_generation == 1
        assert run.plan["run_generation"] == 1

    # Missing distributed presence is a wait: checked at the one bounded,
    # durable rate, never counted toward the degraded cooldown however long it
    # lasts. Restart the coordinator between checks to prove SQL owns the due
    # time.
    from vonk_control.recovery_policy import DISTRIBUTED_RECOVERY

    for _check in range(DISTRIBUTED_RECOVERY.max_failures * 2):
        with sessions() as session:
            run = session.get(RecipeRun, started.owner_id)
            assert run is not None
            assert run.recovery_attempts == 0
            assert "degraded" not in (run.route_error or "")
            assert run.next_action_at is not None
            due = run.next_action_at.replace(tzinfo=NOW.tzinfo)
            assert now[0] < due <= now[0] + timedelta(seconds=15)
            assert run.run_generation == 1
        recovery = DistributedRecoveryCoordinator(
            sessions, routes=routes, agent_jobs=queue, clock=lambda: now[0]
        )
        assert recovery.tick() is False
        now[0] = due
        assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.next_action_at is not None
        now[0] = run.next_action_at.replace(tzinfo=NOW.tzinfo)
    with sessions.begin() as session:
        for presence in session.scalars(select(AgentNode)):
            presence.management_observed_at = now[0]
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: now[0]
    )

    assert recovery.tick() is True
    with sessions() as session:
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == started.owner_id,
            )
        )
        run = session.get(RecipeRun, started.owner_id)
        assert stop is not None and run is not None
        assert run.run_generation == 2


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
        # No Spark reported the rank gone: lost (stoppable, claims held).
        assert run is not None and run.state == "lost"
        assert "one-shot job may have completed external effects" in (
            run.route_error or ""
        )
        assert "submit a new authorized run" in (run.route_error or "")


@pytest.mark.parametrize(
    "invalid", ["missing", "missing-job", "malformed", "unbounded", "wrong-plan"]
)
@pytest.mark.usefixtures("damaged_json_rows")
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
        # Rank 0 still runs on the first Spark: the run is not recovered, but
        # it stays stoppable for whoever needs that Spark next.
        assert run.state == "lost"
        assert run.route_state == "withdrawn"
        assert run.route_error is not None and "start authority" in run.route_error
        assert not tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))


def test_recovery_marker_retains_timestamp_spelling_bound_to_request_identity() -> None:
    deadline = (NOW + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    marker = _recovery_marker(
        {
            "recovery": {
                "schema_version": 1,
                "failed_rank": 0,
                "deadline": deadline,
            }
        }
    )
    assert marker is not None and marker.deadline == deadline
    assert enforce_recovery_deadline(marker, now=NOW)
    with pytest.raises(Exception) as _ending:
        enforce_recovery_deadline(
            {
                "recovery": {
                    "schema_version": 1,
                    "failed_rank": True,
                    "deadline": deadline,
                }
            },
            now=NOW,
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_malformed_plan_waits_for_changed_content_without_repeating_decision(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches unchanged damaged plans bypassing cooldown and re-parsing forever."""
    from vonk_agent_protocol import InvalidRequestReason, WaitResumeCondition
    from vonk_control.distributed_recovery import common
    from vonk_control.persisted_waits import read_wait
    from vonk_control.recovery_policy import DISTRIBUTED_RECOVERY

    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="class-e-install"
    )
    started = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="class-e-start"
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        original = dict(run.plan)
        run.plan = {"damaged": True}
        run.recovery_attempts = DISTRIBUTED_RECOVERY.max_failures
    coordinator = DistributedRecoveryCoordinator(
        sessions,
        routes=bind_route_publications(sessions, service, ConcurrentPublisher())[1],
        agent_jobs=queue,
        clock=lambda: NOW,
    )
    parsed = []
    parser = common.parse_stored_run_plan

    def observe(document):
        parsed.append(document)
        return parser(document)

    monkeypatch.setattr(common, "parse_stored_run_plan", observe)
    monkeypatch.setattr(
        "vonk_control.distributed_recovery.coordinator.parse_stored_run_plan", observe
    )
    with sessions.begin() as session:
        assert coordinator._settle_unreadable_runs(session, NOW, started.owner_id)
    with sessions.begin() as session:
        assert not coordinator._settle_unreadable_runs(session, NOW, started.owner_id)
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        wait = read_wait(run)
        assert wait is not None and wait.next_check > NOW
        assert wait.reason == InvalidRequestReason.MALFORMED
        assert wait.resume_condition == WaitResumeCondition.DEPENDENCY_CHANGED
        assert run.recovery_attempts == DISTRIBUTED_RECOVERY.max_failures
    later = NOW + timedelta(seconds=DISTRIBUTED_RECOVERY.max_delay_seconds + 1)
    with sessions.begin() as session:
        assert coordinator._settle_unreadable_runs(session, later, started.owner_id)
    assert len(parsed) == 1
    assert not coordinator.tick(started.owner_id)
    assert len(parsed) == 1
    with sessions.begin() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        run.plan = original
    with sessions.begin() as session:
        assert coordinator._settle_unreadable_runs(session, later, started.owner_id)
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and read_wait(run) is None
    assert len(parsed) == 2
