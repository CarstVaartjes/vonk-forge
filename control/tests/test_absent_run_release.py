"""A run its Spark reports gone stops holding ports and memory."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    bind_route_publications,
    installed_recipe,
    setup_services,
    started_recipe,
)


def _run(session, run_id: str) -> RecipeRun:
    run = session.get(RecipeRun, run_id)
    assert run is not None
    return run


def _report_absent(sessions, run_id: str) -> None:
    """The Spark's own report: this run is not running (current generation)."""

    with sessions.begin() as session:
        run = _run(session, run_id)
        for node in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            node.state = "failed"
            node.observed_run_generation = run.run_generation
            node.observation_process_running = False
            node.observation_observed_at = NOW - timedelta(seconds=1)
            node.observation_endpoint_ready = (
                False if node.role == "entrypoint" else None
            )


def _active_run_claims(sessions, run_id: str) -> int:
    with sessions() as session:
        return len(
            list(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == run_id,
                        ResourceReservation.state == "active",
                    )
                )
            )
        )


def test_stop_of_a_run_the_spark_reports_gone_succeeds_and_releases(tmp_path):
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path)
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    assert _active_run_claims(sessions, started.owner_id) > 0
    _report_absent(sessions, started.owner_id)

    plan = service.preview_stop(started.owner_id)
    stop = service.stop(
        started.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="s" * 36,
    )

    assert stop.state == "succeeded"
    assert _active_run_claims(sessions, started.owner_id) == 0
    with sessions() as session:
        assert _run(session, started.owner_id).state == "stopped"
        assert not list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
            )
        )


def test_superseded_run_reported_gone_is_released_not_recovered(tmp_path):
    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, distributed_lifecycle=True
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    _report_absent(sessions, started.owner_id)
    with sessions.begin() as session:
        for node in session.scalars(select(AgentNode)):
            node.workload_intent_ordinal += 1  # a newer intent owns the Spark
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )

    assert recovery.tick() is True

    assert _active_run_claims(sessions, started.owner_id) == 0
    with sessions() as session:
        assert _run(session, started.owner_id).state == "stopped"
        assert not list(session.scalars(select(Job).where(Job.kind == "recipe.stop")))


def test_superseded_run_without_a_gone_report_keeps_its_claims(tmp_path):
    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, distributed_lifecycle=True
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    with sessions.begin() as session:
        for node in session.scalars(select(RunNode)):
            node.state = "failed"  # failed, but no Spark has said it is gone
        for agent in session.scalars(select(AgentNode)):
            agent.workload_intent_ordinal += 1
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )

    recovery.tick()

    assert _active_run_claims(sessions, started.owner_id) > 0
    with sessions() as session:
        assert _run(session, started.owner_id).state == "running"
