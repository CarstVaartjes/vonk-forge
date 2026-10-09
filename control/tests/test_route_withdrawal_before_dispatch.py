"""A Stop or recovery withdraws the route, then dispatches, in short transactions.

The bundle activation and the supervisor acknowledgement are external effects.
They run with no transaction open between a claim and a conditional completion;
the Stop (or recovery) is dispatched in its own short transaction, and only
while the withdrawal is still complete.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import LifecycleState, ProjectionCode, RouteState, RunState
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.models import AgentOperation, AgentPresence, Job, RecipeRun, RunNode
from vonk_control.offline_stops import deferred_stop_nodes
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import (
    STOP_DISPATCH_GRACE,
    AtomicRecipeRoutePublisher,
    RecipeRouteService,
)
from vonk_control.route_runtime import (
    ActivationMarker,
    AtomicRouteBundlePublisher,
)

from .non_blocking import assert_ended_without_blocking
from .test_recipe_operations import (
    NOW,
    _required,
    installed_recipe,
    record_exact_empty_snapshot,
    setup_services,
    started_recipe,
)


class _Gate:
    """Acknowledge instantly, except one armed wait a test holds or kills."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.die = False
        self._armed = False

    def arm(self) -> None:
        self.entered.clear()
        self.release.clear()
        self._armed = True

    def __call__(self, _marker: ActivationMarker) -> None:
        if not self._armed:
            return
        self._armed = False
        self.entered.set()
        assert self.release.wait(timeout=60), "the test never released the wait"
        if self.die:
            raise _Crash


class _Crash(BaseException):
    """A process death: nothing in the Controller may catch it."""


def _routes(sessions, root: Path, gate: _Gate) -> RecipeRouteService:
    return RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(
            AtomicRouteBundlePublisher(root, await_supervisor_ack=gate)
        ),
        management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=120,
    )


def _aliases(root: Path) -> set[str]:
    bundle = _verified_bundle(root).routes
    assert bundle is not None
    return set(bundle.routes)


def _world(tmp_path: Path, *, nodes: int = 1, distributed: bool = False, engine=None):
    sessions, service, queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes, distributed_lifecycle=distributed, engine=engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id="a" * 36
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        node_ids,
        request_id="b" * 36,
        alias="qwen",
    )
    gate = _Gate()
    root = tmp_path / "live"
    routes = _routes(sessions, root, gate)
    stopper = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=service._agent_jobs,
        clock=lambda: NOW,
        route_publications=routes,
    )
    routes.publish_run(run.owner_id)
    assert _aliases(root) == {"qwen"}
    return sessions, stopper, routes, queue, run, gate, root, node_ids


def _owner_lock_is_free(routes: RecipeRouteService) -> None:
    def take() -> None:
        with routes.publication_transaction():
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(take).result(timeout=15)


def _stop_jobs(sessions) -> list[Job]:
    with sessions() as session:
        return list(session.scalars(select(Job).where(Job.kind == "recipe.stop")))


def _stop_waits_without_a_transaction(tmp_path: Path, engine) -> None:
    sessions, service, routes, _queue, run, gate, root, _nodes = _world(
        tmp_path, engine=engine
    )
    plan = service.preview_stop(run.owner_id)

    gate.arm()
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(
            service.stop,
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="c" * 36,
        )
        try:
            assert gate.entered.wait(timeout=30)
            # The supervisor has not acknowledged the withdrawal yet: nothing
            # may hold the owner lock meanwhile, and the Stop is not dispatched.
            _owner_lock_is_free(routes)
            accepted = _stop_jobs(sessions)
            assert len(accepted) == 1 and accepted[0].state == "running"
            assert accepted[0].request_id == "c" * 36
            assert accepted[0].payload["plan_digest"] == plan.plan_digest
            assert accepted[0].payload.get("phases") is None
            with sessions() as session:
                assert (
                    session.scalar(
                        select(AgentOperation.id).where(
                            AgentOperation.parent_job_id == accepted[0].id
                        )
                    )
                    is None
                )
                assert (
                    _required(session.get(RecipeRun, run.owner_id)).state == "running"
                )
        finally:
            gate.release.set()
        stopped = stopping.result(timeout=60)

    assert stopped.id == accepted[0].id
    assert stopped.state in {"queued", "running"}
    assert _aliases(root) == set()
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        assert (stored.state, stored.route_state) == ("stopping", "withdrawn")
    assert len(_stop_jobs(sessions)) == 1


def test_stop_withdraws_without_a_transaction_and_dispatches_only_afterwards(
    tmp_path: Path,
) -> None:
    _stop_waits_without_a_transaction(tmp_path, None)


def test_postgres_stop_withdraws_without_holding_the_owner_row_lock(
    tmp_path: Path, postgres_engine
) -> None:
    _stop_waits_without_a_transaction(tmp_path, postgres_engine)


def test_stop_withdraws_again_when_a_competing_publication_lists_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, service, routes, _queue, run, _gate, root, _nodes = _world(tmp_path)
    plan = service.preview_stop(run.owner_id)
    withdraw = routes.withdraw_run
    calls: list[str] = []

    def withdraw_then_lose_the_race(run_id: str, **kwargs):
        calls.append(run_id)
        generation = withdraw(run_id, **kwargs)
        if len(calls) == 1:
            routes.publish_run(run_id)  # a competing change lists the run again
            assert _aliases(root) == {"qwen"}
        return generation

    monkeypatch.setattr(routes, "withdraw_run", withdraw_then_lose_the_race)

    service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )

    # The dispatch is conditional on the withdrawal still being complete.
    assert len(calls) == 2
    assert _aliases(root) == set()
    assert len(_stop_jobs(sessions)) == 1


def test_a_stop_killed_while_withdrawing_resumes_from_the_claim(
    tmp_path: Path,
) -> None:
    sessions, service, _routes, _queue, run, gate, root, _nodes = _world(tmp_path)
    plan = service.preview_stop(run.owner_id)

    gate.arm()
    gate.die = True
    gate.release.set()
    with pytest.raises(_Crash):
        service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="e" * 36,
        )

    # Consent survived; the completion and native dispatch did not.
    accepted = _stop_jobs(sessions)
    assert len(accepted) == 1 and accepted[0].state == "running"
    assert accepted[0].payload.get("phases") is None
    with sessions() as session:
        assert _required(session.get(RecipeRun, run.owner_id)).state == "running"

    gate.die = False
    resumed = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )
    assert resumed.id == accepted[0].id
    assert _aliases(root) == set()
    assert len(_stop_jobs(sessions)) == 1


def test_a_refused_stop_withdraws_nothing(tmp_path: Path) -> None:
    sessions, service, _routes, _queue, run, _gate, root, _nodes = _world(tmp_path)
    plan = service.preview_stop(run.owner_id)
    with sessions.begin() as session:
        _required(session.get(RecipeRun, run.owner_id)).state = "stopped"

    from .observed_actions import observe_action

    observe_action(
        lambda: service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="f" * 36,
        )
    )

    assert _aliases(root) == {"qwen"}
    assert _stop_jobs(sessions) == []
    with sessions.begin() as session:
        _required(session.get(RecipeRun, run.owner_id)).state = RunState.RUNNING
    fresh_plan = service.preview_stop(run.owner_id)
    service.stop(
        run.owner_id,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id="z" * 36,
    )
    assert _aliases(root) == set()


def _failed_rank_world(tmp_path: Path, engine=None):
    sessions, _service, routes, queue, run, gate, root, nodes = _world(
        tmp_path, nodes=2, distributed=True, engine=engine
    )
    record_exact_empty_snapshot(sessions, nodes[1], NOW + timedelta(seconds=1))
    with sessions.begin() as session:
        for presence in session.scalars(select(AgentPresence)):
            session.delete(presence)
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )
    return sessions, routes, recovery, run, gate, root


def _recovery_waits_without_a_transaction(tmp_path: Path, engine) -> None:
    sessions, routes, recovery, run, gate, root = _failed_rank_world(tmp_path, engine)

    gate.arm()
    with ThreadPoolExecutor(max_workers=1) as pool:
        ticking = pool.submit(recovery.tick)
        try:
            assert gate.entered.wait(timeout=30)
            _owner_lock_is_free(routes)
        finally:
            gate.release.set()
        assert ticking.result(timeout=60) is True

    assert _aliases(root) == set()
    with sessions() as session:
        assert (
            _required(session.get(RecipeRun, run.owner_id)).route_state == "withdrawn"
        )


def test_recovery_withdraws_without_a_transaction_then_continues(
    tmp_path: Path,
) -> None:
    _recovery_waits_without_a_transaction(tmp_path, None)


def test_postgres_recovery_withdraws_without_holding_the_owner_row_lock(
    tmp_path: Path, postgres_engine
) -> None:
    _recovery_waits_without_a_transaction(tmp_path, postgres_engine)


def test_recovery_killed_while_withdrawing_resumes_from_the_claim(
    tmp_path: Path,
) -> None:
    sessions, _routes, recovery, run, gate, root = _failed_rank_world(tmp_path)

    gate.arm()
    gate.die = True
    gate.release.set()
    with pytest.raises(_Crash):
        recovery.tick()
    # Intent is durable; the live bundle is what the tick must still converge.
    with sessions() as session:
        assert (
            _required(session.get(RecipeRun, run.owner_id)).route_state == "withdrawn"
        )

    gate.die = False
    recovery.tick()

    assert _aliases(root) == set()


def test_accepted_stop_continues_without_client_and_never_republishes(
    tmp_path: Path,
) -> None:
    sessions, service, routes, _queue, run, gate, root, _nodes = _world(tmp_path)
    plan = service.preview_stop(run.owner_id)
    gate.arm()
    gate.die = True
    gate.release.set()
    with pytest.raises(_Crash):
        service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="g" * 36,
        )
    # The client is gone. The run still runs and says why its route is withdrawn.
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        assert (stored.state, stored.route_state) == ("running", "withdrawn")
        assert _aliases(root) == set()

    # Within the grace period the Controller leaves the Stop its chance.
    routes._clock = lambda: NOW + timedelta(minutes=1)
    routes.maintain()
    with sessions() as session:
        assert _required(session.get(RecipeRun, run.owner_id)).state == RunState.RUNNING
        assert _aliases(root) == set()

    later = NOW + STOP_DISPATCH_GRACE + timedelta(minutes=1)
    routes._clock = lambda: later
    with sessions.begin() as session:  # the Sparks keep reporting meanwhile
        for node in session.scalars(
            select(RunNode).where(RunNode.run_id == run.owner_id)
        ):
            node.updated_at = later
    accepted = _stop_jobs(sessions)
    assert len(accepted) == 1 and accepted[0].payload.get("phases") is None
    routes.maintain()
    assert _aliases(root) == set()
    gate.die = False
    service._clock = lambda: later
    worker = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: later,
        stop_admission_cleanup=service.reconcile_pending_service_stops,
    )
    for _ in range(3):
        worker.tick()

    assert _aliases(root) == set()
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        assert (stored.state, stored.route_state) == (
            RunState.STOPPING,
            RouteState.WITHDRAWN,
        )
        assert stored.stopped_at is None
        assert (
            stored.route_error
            == f"{ProjectionCode.NODE_OFFLINE}: exact Stop pending reconnect"
        )
    continued = _stop_jobs(sessions)
    assert len(continued) == 1 and continued[0].id == accepted[0].id
    assert continued[0].payload.get("phases")
    assert continued[0].state == LifecycleState.SUCCEEDED
    assert deferred_stop_nodes(continued[0]) == frozenset(continued[0].targets)
    # Foreground completion is not a busy gate for a fresh exact Stop request.
    fresh_plan = service.preview_stop(run.owner_id)
    with sessions() as session:
        ended, fresh = assert_ended_without_blocking(
            session,
            service.get(continued[0].id),
            end=lambda original: service.get(original.id),
            fresh=lambda _world: service.stop(
                run.owner_id,
                plan_digest=fresh_plan.plan_digest,
                actor="admin",
                request_id="h" * 36,
            ),
            request_key=lambda receipt: (
                _required(session.get(Job, receipt.id)).request_id
            ),
        )
    assert ended.state == LifecycleState.SUCCEEDED
    assert fresh.id != continued[0].id
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}


from .test_route_runtime import _verified_bundle


@pytest.mark.parametrize(
    "failure",
    [
        "busy",
        "storage-permission",
        "receipt-digest",
        "receipt-shape",
        "runtime-generation",
    ],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_unknown_withdrawal_continues_accepted_stop_and_admits_fresh_request(
    tmp_path, monkeypatch, failure
):
    """A local withdrawal miss must retain consent and resume without the client."""
    from vonk_control.recipe_routes import RecipeRouteNotReady

    sessions, service, routes, _queue, run, _gate, root, _nodes = _world(tmp_path)
    plan = service.preview_stop(run.owner_id)
    withdraw = routes.withdraw_run

    def unavailable(*args, **kwargs):
        if failure == "storage-permission":
            raise PermissionError("route storage temporarily unavailable")
        raise RecipeRouteNotReady("publication owner busy")

    monkeypatch.setattr(routes, "withdraw_run", unavailable)
    accepted = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="u" * 36,
    )
    assert accepted.state == LifecycleState.RUNNING
    assert _aliases(root)
    with sessions() as session:
        assert not tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted.id
                )
            )
        )
    monkeypatch.setattr(routes, "withdraw_run", withdraw)
    if failure in {"receipt-digest", "receipt-shape", "runtime-generation"}:
        with sessions.begin() as session:
            parent = _required(session.get(Job, accepted.id))
            original_digest, original_payload = parent.payload_digest, parent.payload
            stored = _required(session.get(RecipeRun, run.owner_id))
            original_generation = stored.run_generation
            if failure == "receipt-digest":
                parent.payload_digest = "f" * 64
            elif failure == "receipt-shape":
                parent.payload = {**parent.payload, "owner_kind": "unreadable"}
            else:
                stored.run_generation += 1
        service._clock = lambda: NOW + timedelta(seconds=6)
        RecipeOperationWorker(
            sessions,
            routes,
            clock=service._clock,
            stop_admission_cleanup=service.reconcile_pending_service_stops,
        ).tick()
        assert service.get(accepted.id).state == LifecycleState.RUNNING
        assert _aliases(root) == {"qwen"}
        with sessions.begin() as session:
            parent = _required(session.get(Job, accepted.id))
            parent.payload_digest, parent.payload = original_digest, original_payload
            _required(
                session.get(RecipeRun, run.owner_id)
            ).run_generation = original_generation
    service._clock = lambda: NOW + timedelta(seconds=12)
    assert service.reconcile_pending_service_stops()
    assert _aliases(root) == set()
    continued = service.get(accepted.id)
    assert continued.id == accepted.id
    with sessions() as session:
        dispatched = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted.id
                )
            )
        )
    for operation in dispatched:
        service.record_node_result(
            accepted.id, operation.node_id, succeeded=True, evidence={}
        )
    with sessions() as session:
        installation_id = _required(
            session.get(RecipeRun, run.owner_id)
        ).installation_id
        ended, fresh = assert_ended_without_blocking(
            session,
            service.get(accepted.id),
            end=lambda original: service.get(original.id),
            fresh=lambda _world: started_recipe(
                sessions,
                service,
                installation_id,
                _nodes,
                request_id="v" * 36,
                alias="fresh",
            ),
            request_key=lambda receipt: (
                _required(session.get(Job, receipt.id)).request_id
            ),
        )
    assert ended.id == accepted.id
    assert fresh.id != accepted.id
    assert fresh.state in {
        LifecycleState.RUNNING,
        LifecycleState.QUEUED,
        LifecycleState.SUCCEEDED,
    }


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", ["shape", "missing", "empty"])
def test_unreadable_stop_expires_without_effect_and_fresh_stop_is_admitted(
    tmp_path, monkeypatch, damage
):
    """Malformed intent must not bypass expiry and strand the next accepted Stop."""
    from vonk_control.agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
    from vonk_control.recipe_routes import RecipeRouteNotReady

    sessions, service, routes, _queue, run, _gate, root, _nodes = _world(tmp_path)
    original = routes.withdraw_run

    def unavailable(*args, **kwargs):
        raise RecipeRouteNotReady("publication owner busy")

    monkeypatch.setattr(routes, "withdraw_run", unavailable)
    plan = service.preview_stop(run.owner_id)
    accepted = service.stop(
        run.owner_id, plan_digest=plan.plan_digest, actor="admin", request_id="w" * 36
    )
    with sessions.begin() as session:
        parent = _required(session.get(Job, accepted.id))
        if damage == "shape":
            parent.payload = {**parent.payload, "owner_kind": "unreadable"}
        elif damage == "missing":
            session.execute(update(Job).where(Job.id == parent.id).values(payload=None))
        else:
            parent.payload = {}
    monkeypatch.setattr(routes, "withdraw_run", original)
    later = NOW + timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS + 1)
    service._clock = lambda: later
    worker = RecipeOperationWorker(
        sessions,
        routes,
        clock=service._clock,
        stop_admission_cleanup=service.reconcile_pending_service_stops,
    )
    assert worker.tick()
    assert _aliases(root) == {"qwen"}
    with sessions() as session:
        assert _required(session.get(RecipeRun, run.owner_id)).state == RunState.RUNNING
        assert not tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted.id
                )
            )
        )
    plan = service.preview_stop(run.owner_id)

    def assert_no_effect(_receipt):
        assert _aliases(root) == {"qwen"}

    with sessions() as session:
        ended, fresh = assert_ended_without_blocking(
            session,
            service.get(accepted.id),
            end=lambda receipt: service.get(receipt.id),
            fresh=lambda _world: service.stop(
                run.owner_id,
                plan_digest=plan.plan_digest,
                actor="admin",
                request_id="x" * 36,
            ),
            request_key=lambda receipt: (
                _required(session.get(Job, receipt.id)).request_id
            ),
            assert_reason=assert_no_effect,
        )
    assert ended.state == LifecycleState.FAILED
    assert fresh.id != ended.id
    assert _aliases(root) == set()
