"""RecipeRun MVCC fences preserve current intent and roll back child effects."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import RouteState, RunState, UnknownOutcomeError, WaitReason
from vonk_control.models import RecipeRun, RunNode
from vonk_control.optimistic import Applied, transition
from vonk_control.recipe_operation_worker import RecipeOperationWorker

from .test_recipe_routes import NOW, setup


def test_concurrent_run_decisions_replan_and_rollback_rank_effects(
    postgres_engine, tmp_path
) -> None:
    """Catches lost run updates and child-only effects escaping a stale decision."""
    routes, _publisher, _applied, run_id = setup(tmp_path, engine=postgres_engine)
    sessions = routes.sessions
    read = threading.Barrier(2)
    committed = threading.Event()
    seen: list[str] = []

    def winner(session: Session) -> None:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        read.wait(timeout=30)
        run.state = RunState.STOPPED

    def loser(session: Session) -> bool:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        seen.append(run.state)
        if run.state == RunState.STOPPED:
            return False
        read.wait(timeout=30)
        assert committed.wait(timeout=30)
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None
        rank.state = RunState.FAILED
        run.route_error = "obsolete decision"
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        losing = pool.submit(transition, sessions, loser)
        winning = pool.submit(transition, sessions, winner)
        assert isinstance(winning.result(timeout=30), Applied)
        committed.set()
        outcome = losing.result(timeout=30)
    assert isinstance(outcome, Applied) and outcome.value is False
    assert seen == [RunState.RUNNING, RunState.STOPPED]
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == RunState.STOPPED
        assert run.version == 2 and run.route_error is None
        assert set(session.scalars(select(RunNode.state))) == {RunState.RUNNING}


@pytest.mark.parametrize("action", ["end", "defer", "claim"])
def test_busy_run_yields_typed_wait_and_next_tick_recovers(
    postgres_engine, tmp_path, action
) -> None:
    """Catches blocking FOR UPDATE and a stale result retried by an outer loop."""
    routes, _publisher, applied, run_id = setup(tmp_path, engine=postgres_engine)
    worker = RecipeOperationWorker(routes.sessions, routes=routes, clock=lambda: NOW)
    with routes.sessions() as session:
        original = session.get(RecipeRun, run_id)
        assert original is not None and original.route_state == RouteState.PENDING

    def report() -> None:
        if action == "end":
            worker._end_publication(run_id, RuntimeError("superseded"))
        elif action == "defer":
            worker._defer_publication(
                run_id,
                UnknownOutcomeError("rank pending", reason=WaitReason.SCOPE_CHANGED),
            )
        else:
            routes.publish_run(run_id)

    # Release the injected holder even when the future times out, so a broken
    # implementation fails without deadlocking the executor's shutdown.
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        routes.sessions.begin() as holder,
    ):
        run = holder.get(RecipeRun, run_id)
        assert run is not None
        run.version += 1
        holder.flush()
        with pytest.raises(UnknownOutcomeError) as caught:
            pool.submit(report).result(timeout=10)
        assert caught.value.typed_reason == WaitReason.SCOPE_CHANGED
    assert applied == []
    # Neither reset nor a new request is needed once the held writer commits.
    report()
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert run.route_state == (
            RouteState.WITHDRAWN
            if action == "end"
            else RouteState.PENDING
            if action == "defer"
            else RouteState.PUBLISHED
        )
        if action == "defer":
            assert run.next_action_at is not None and run.route_error


def test_startup_adopts_version_and_preserves_running_run(
    postgres_engine, tmp_path
) -> None:
    """Catches destructive adoption or a nullable/defaultless column on restart."""
    from vonk_control.db.startup import verify_schema_is_current

    routes, _publisher, _applied, run_id = setup(tmp_path, engine=postgres_engine)
    routes.publish_run(run_id)
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        identity = (run.run_generation, run.plan_digest, run.route_digest)
        rank_ids = tuple(session.scalars(select(RunNode.id).order_by(RunNode.id)))
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE recipe_runs DROP COLUMN version")
        verify_schema_is_current(connection)
    # A second startup is idempotent, and an established workload stays live.
    with postgres_engine.begin() as connection:
        verify_schema_is_current(connection)
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.version == 1
        assert run.state == RunState.RUNNING
        assert run.route_state == RouteState.PUBLISHED
        assert (run.run_generation, run.plan_digest, run.route_digest) == identity
        assert (
            tuple(session.scalars(select(RunNode.id).order_by(RunNode.id))) == rank_ids
        )
    routes.withdraw_run(run_id)
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.version > 1
        assert run.route_state == RouteState.WITHDRAWN
