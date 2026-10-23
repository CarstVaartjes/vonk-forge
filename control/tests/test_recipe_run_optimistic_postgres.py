"""RecipeRun MVCC fences preserve current intent and roll back child effects."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    Disposition,
    RouteState,
    RunState,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control.failure_classification import disposition
from vonk_control.models import RecipeRun, RoutePublicationOwner, RunNode
from vonk_control.optimistic import Applied, transition
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recovery_policy import ROUTE_RECOVERY

from .test_recipe_routes import NOW, MutableClock, setup


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
        assert run.version == 2
        assert set(session.scalars(select(RunNode.state))) == {RunState.RUNNING}


@pytest.mark.parametrize("action", ["end", "defer", "claim"])
def test_busy_run_yields_typed_wait_and_next_tick_recovers(
    postgres_engine, tmp_path, action
) -> None:
    """Catches blocking FOR UPDATE and a stale result retried by an outer loop."""
    routes, _publisher, applied, run_id = setup(tmp_path, engine=postgres_engine)
    worker = RecipeOperationWorker(routes.sessions, routes=routes, clock=lambda: NOW)
    with routes.sessions.begin() as session:
        original = session.get(RecipeRun, run_id)
        assert original is not None
        original.route_state = RouteState.PENDING

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


def test_rank_contention_beyond_publication_budget_waits_then_publishes(
    postgres_engine, tmp_path
) -> None:
    """Catches OBSERVE spending publication retries on a busy rank writer."""
    routes, _runtime, applied, run_id = setup(tmp_path, engine=postgres_engine)
    worker = RecipeOperationWorker(routes.sessions, routes, clock=lambda: NOW)
    with routes.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.route_state = RouteState.PENDING
    with routes.sessions.begin() as holder:
        holder.scalar(select(RunNode).where(RunNode.run_id == run_id).with_for_update())
        for _ in range(ROUTE_RECOVERY.max_failures + 2):
            assert worker._publish_runs((run_id,))
            with routes.sessions() as session:
                run = session.get(RecipeRun, run_id)
                assert run is not None
                assert run.route_state == RouteState.PENDING
                assert run.route_attempts == 0
                assert run.next_action_at is not None and run.next_action_at > NOW
                assert run.route_error is not None
                assert run.route_generation is None
        assert applied == []
    assert worker._publish_runs((run_id,))
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == RouteState.PUBLISHED
        assert run.route_generation is not None
    assert len(applied) == 1


def test_owner_contention_after_activation_keeps_completion_and_generation(
    postgres_engine, tmp_path, monkeypatch
) -> None:
    """Catches dropping the completion decision on the first NOWAIT refusal."""
    from sqlalchemy.exc import OperationalError
    from vonk_control.recipe_routes import service as service_module

    routes, runtime, applied, run_id = setup(tmp_path, engine=postgres_engine)
    activated = threading.Event()
    held = threading.Event()
    contended = threading.Event()
    released = threading.Event()
    original_lock = service_module.lock_route_publication_owner_in_session
    completions = []
    original_completion = routes._record_completion

    def acknowledge(_marker) -> None:
        activated.set()
        assert held.wait(timeout=10)

    def acquire(session):
        try:
            return original_lock(session)
        except OperationalError:
            contended.set()
            assert released.wait(timeout=10)
            raise

    def complete(publication, generation):
        completions.append(generation.generation)
        return original_completion(publication, generation)

    runtime._await_supervisor_ack = acknowledge
    monkeypatch.setattr(
        service_module, "lock_route_publication_owner_in_session", acquire
    )
    monkeypatch.setattr(routes, "_record_completion", complete)
    with ThreadPoolExecutor(max_workers=1) as pool:
        publishing = pool.submit(routes.publish_run, run_id)
        try:
            assert activated.wait(timeout=10)
            with routes.sessions.begin() as holder:
                holder.scalar(select(RoutePublicationOwner).with_for_update())
                held.set()
                assert contended.wait(timeout=10)
        finally:
            held.set()
            released.set()
        generation = publishing.result(timeout=10)
    assert completions == [generation.generation]
    assert routes.publish_run(run_id).generation == generation.generation
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == RouteState.PUBLISHED
        assert run.route_generation == generation.generation
    assert len(list((tmp_path / "litellm/generations").iterdir())) == 1
    assert applied


def test_unwrapped_stale_writer_waits_without_spending_publication_budget(
    postgres_engine, tmp_path
) -> None:
    """Catches raw mapper conflicts outside transition being deterministic FAIL."""
    from sqlalchemy.orm.exc import StaleDataError

    routes, _runtime, _applied, run_id = setup(tmp_path, engine=postgres_engine)
    worker = RecipeOperationWorker(routes.sessions, routes, clock=lambda: NOW)
    with routes.sessions() as loser:
        stale = loser.get(RecipeRun, run_id)
        assert stale is not None
        with routes.sessions.begin() as winner:
            run = winner.get(RecipeRun, run_id)
            assert run is not None
            run.route_state = RouteState.PENDING
            run.route_error = "new observation"
        stale.route_error = "obsolete offline stop"
        with pytest.raises(StaleDataError) as caught:
            loser.commit()
        loser.rollback()
    assert disposition(caught.value) == Disposition.WAIT
    for _ in range(ROUTE_RECOVERY.max_failures + 2):
        worker._defer_publication(run_id, caught.value)
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == RouteState.PENDING
        assert run.route_attempts == 0 and run.next_action_at is not None
    assert worker._publish_runs((run_id,))


def test_deadline_contention_is_visible_wait_and_heals_next_tick(
    postgres_engine, tmp_path, caplog
) -> None:
    """Catches a stale expiry reaching _advance's generic traceback handler."""
    import json
    import logging
    from datetime import timedelta

    from vonk_control.models import AgentNode

    routes, _publisher, _applied, run_id = setup(tmp_path, engine=postgres_engine)
    now = [NOW]
    with routes.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.route_state = RouteState.PENDING
        run.observation_deadline_at = NOW
        for rank in session.scalars(select(RunNode)):
            rank.observed_run_generation = None
            rank.observation_observed_at = None
        for node in session.scalars(select(AgentNode)):
            node.last_seen_at = NOW
    worker = RecipeOperationWorker(
        routes.sessions, routes, clock=lambda: now[0], reconcile_routes=False
    )
    caplog.set_level(logging.INFO)
    with routes.sessions.begin() as holder:
        run = holder.get(RecipeRun, run_id)
        assert run is not None
        run.version += 1
        holder.flush()
        assert worker.tick() is False
    waits = [json.loads(record.message) for record in caplog.records]
    assert len(waits) == 1
    assert waits[0]["reason"] == WaitReason.SCOPE_CHANGED
    assert waits[0]["retry_at"]
    assert all(record.exc_info is None for record in caplog.records)
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == RouteState.PENDING
    assert worker.tick() is False  # the logged next-attempt time is enforced
    now[0] += timedelta(seconds=5)
    assert worker.tick() is True
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == RouteState.WITHDRAWN
        assert run.state == RunState.RUNNING


def test_publication_deferral_contention_continues_without_traceback(
    postgres_engine, tmp_path, monkeypatch, caplog
) -> None:
    """Catches a stale deferral logged as a failure or starving the next run."""
    import json
    import logging

    from .test_recipe_routes import add_running_run

    routes, _publisher, _applied, run_id = setup(tmp_path, engine=postgres_engine)
    later = add_running_run(
        routes, run_id, alias="later", route_state=RouteState.PENDING, identity=3
    )
    with routes.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.route_state = RouteState.PENDING
    publish = routes.publish_run
    visited = []

    def unavailable_first(candidate):
        visited.append(candidate)
        if candidate == run_id:
            raise ConnectionError("publisher temporarily unavailable")
        return publish(candidate)

    monkeypatch.setattr(routes, "publish_run", unavailable_first)
    worker = RecipeOperationWorker(routes.sessions, routes, clock=lambda: NOW)
    caplog.set_level(logging.INFO)
    with routes.sessions.begin() as holder:
        run = holder.get(RecipeRun, run_id)
        assert run is not None
        run.version += 1
        holder.flush()
        assert worker._publish_runs((run_id, later)) is True
    assert visited == [run_id, later]
    waits = [json.loads(record.message) for record in caplog.records]
    assert any(wait.get("reason") == WaitReason.SCOPE_CHANGED for wait in waits)
    assert all(record.exc_info is None for record in caplog.records)
    monkeypatch.setattr(routes, "publish_run", publish)
    assert worker._publish_runs((run_id,)) is True
    with routes.sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == RouteState.PUBLISHED


def test_order_sweep_conflict_rolls_back_continues_and_resweeps(
    postgres_engine, tmp_path, monkeypatch, caplog
) -> None:
    """Catches raw receipt-consumer conflicts aborting a pass past its cursor."""
    import json
    import logging
    from datetime import timedelta
    from unittest.mock import Mock

    from vonk_agent_protocol import (
        AgentOperation as OperationKind,
    )
    from vonk_agent_protocol import (
        AgentResult,
        AgentResultState,
        LifecycleState,
    )
    from vonk_agent_protocol.recipe_operations import RecipeStopResult
    from vonk_control import agent_operation_states as aos
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.agent_jobs import results as ingress
    from vonk_control.models import AgentNode, AgentOperation, AgentOperationAttempt

    from .agent_fences import fenced_attempt
    from .recipe_stop_fixtures import recipe_stop_payload
    from .runtime_identity_support import claim_agent
    from .test_agent_jobs import COMMIT, parent
    from .test_recipe_operations import NOW as OPERATION_NOW
    from .test_recipe_operations import (
        _issue_exact_stop_grant,
        installed_recipe,
        setup_services,
        started_recipe,
    )

    sessions, operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, mapping_node_count=1, engine=postgres_engine
    )
    installation = installed_recipe(
        operations, mapping_id, build_id, nodes[:1], request_id="i" * 36
    )
    started = started_recipe(
        sessions,
        operations,
        installation.owner_id,
        nodes[:1],
        request_id="r" * 36,
        alias="sweep-conflict",
    )
    stop_plan = operations.preview_stop(started.owner_id)
    stopped = operations.stop(
        started.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="s" * 36,
    )
    jobs = AgentJobService(sessions, clock=lambda: OPERATION_NOW)
    sibling_parent = parent(sessions, MutableClock(OPERATION_NOW))
    with sessions.begin() as session:
        stored_parent = session.get(type(sibling_parent), sibling_parent.id)
        assert stored_parent is not None
        stored_parent.targets = [nodes[1]]
        node = session.get(AgentNode, nodes[1])
        assert node is not None
        node.workload_intent_ordinal = 1
        jobs.enqueue_in_session(
            session,
            sibling_parent.id,
            nodes[1],
            OperationKind.RECIPE_STOP,
            COMMIT,
            recipe_stop_payload(nodes[1], plan_digest=COMMIT),
            operation_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        )
    first_claim = _issue_exact_stop_grant(
        sessions, node_id=nodes[0], certificate_serial="serial-0"
    )[0]
    later_claim = claim_agent(jobs, nodes[1], "serial-1")
    assert later_claim is not None
    claims = [first_claim, later_claim]
    now = max(
        fenced_attempt(sessions, claim).lease_deadline for claim in claims
    ) + timedelta(seconds=1)
    jobs = AgentJobService(sessions, clock=lambda: now)
    jobs.set_result_consumer(operations.consume_agent_result)
    # Exercise the authenticated retained-receipt producer, with its projection
    # interrupted. No agent reconnects during either subsequent sweep.
    with monkeypatch.context() as fault:
        fault.setattr(ingress, "adopt_observation_receipt", lambda *_a, **_kw: False)
        for claim in claims:
            jobs.record_late_result(
                AgentResult(
                    fence=claim.fence,
                    state=AgentResultState.SUCCEEDED,
                    result=RecipeStopResult(),
                )
            )
    with sessions() as session:
        candidates = tuple(
            session.scalars(
                select(AgentOperation)
                .where(
                    AgentOperation.parent_job_id.in_((stopped.id, sibling_parent.id))
                )
                .order_by(AgentOperation.id)
            )
        )
    assert len(candidates) == 2
    first, later = candidates
    visited = []
    consume = operations.consume_agent_result
    injected = False

    def winner():
        with sessions.begin() as session:
            run = session.get(RecipeRun, started.owner_id)
            assert run is not None
            run.route_error = "concurrent current observation"

    def consume_with_writer(session, operation, attempt, message):
        nonlocal injected
        visited.append(operation.id)
        # Hold the old ORM version in the real queue transaction before a
        # separate PostgreSQL writer commits. Production consumption and its
        # mapper flush must lose that fence, rolling back rank/receipt effects.
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        if operation.id == first.id and not injected:
            injected = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(winner).result(timeout=10)
        consume(session, operation, attempt, message)

    from vonk_control.agent_jobs import reconcile as sweep

    completed = []
    refresh = sweep.refresh_queue_waits

    def refresh_after_sweep(*args, **kwargs):
        completed.append(True)
        return refresh(*args, **kwargs)

    monkeypatch.setattr(sweep, "refresh_queue_waits", refresh_after_sweep)
    jobs = AgentJobService(sessions, clock=lambda: now)
    jobs.set_result_consumer(consume_with_writer)
    caplog.set_level(logging.INFO)
    worker = RecipeOperationWorker(
        sessions,
        routes=Mock(),
        clock=lambda: now,  # routes are disabled for this pass
        reconcile_routes=False,
        order_reconcile=jobs.reconcile_orders,
    )
    assert worker.tick() is True
    assert visited[:2] == [first.id, later.id]
    assert completed == [True]  # the final reconcile_orders stage still ran
    with sessions() as session:
        first_after = session.get(AgentOperation, first.id)
        later_after = session.get(AgentOperation, later.id)
        assert first_after is not None and first_after.state in aos.LIVE
        assert later_after is not None and later_after.state == LifecycleState.SUCCEEDED
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == first.id
            )
        )
        assert attempt is not None and aos.attempt_lapsed(attempt)
        assert attempt.result is not None
    waits = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    assert any(
        wait.get("operation_id") == first.id and wait.get("retry_at") for wait in waits
    )
    assert all(record.exc_info is None for record in caplog.records)
    assert worker.tick() is True
    with sessions() as session:
        healed = session.get(AgentOperation, first.id)
        assert healed is not None and healed.state == LifecycleState.SUCCEEDED
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.state == RunState.STOPPED
    fresh_plan = operations.preview_run(installation.owner_id, "after-sweep")
    fresh = operations.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id="f" * 36,
    )
    assert fresh.owner_id != started.owner_id
