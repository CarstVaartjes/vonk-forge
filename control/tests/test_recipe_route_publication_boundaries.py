"""Route publication keeps its database transactions short.

The bundle activation and the supervisor acknowledgement (minutes in the worst
case) are external effects. They run between two short transactions: a claim,
then a completion that only applies while the claim is still current.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from vonk_control.models import RoutePublication
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes import (
    AtomicRecipeRoutePublisher,
    RecipeRouteService,
)
from vonk_control.route_runtime import (
    ActivationMarker,
    AtomicRouteBundlePublisher,
)

from .observed_actions import observe_action
from .test_recipe_routes import (
    NOW,
    MutableClock,
    _publication,
    _publication_owner,
    _recipe_run,
    add_running_run,
    setup,
)
from .test_route_runtime import _verified_bundle


def _service(
    base: RecipeRouteService,
    root: Path,
    clock: Callable[[], datetime],
    acknowledge: Callable[[ActivationMarker], None] | None = None,
) -> RecipeRouteService:
    return RecipeRouteService(
        base.sessions,
        publisher=AtomicRecipeRoutePublisher(
            AtomicRouteBundlePublisher(root, await_supervisor_ack=acknowledge)
        ),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
    )


class _BlockedAcknowledgement:
    """Acknowledge instantly, except one armed wait that a test releases."""

    def __init__(self, *, lose_acknowledgement: bool = False) -> None:
        self.lose_acknowledgement = lose_acknowledgement
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = False
        self.waited: list[int] = []

    def arm(self) -> None:
        self._armed = True

    def __call__(self, marker: ActivationMarker) -> None:
        if not self._armed:
            return
        self._armed = False
        self.waited.append(marker.generation)
        self.entered.set()
        assert self.release.wait(timeout=60), "the test never released the wait"
        if self.lose_acknowledgement:
            raise OSError("superseded acknowledgement connection lost")


def _live_aliases(root: Path) -> set[str]:
    bundle = _verified_bundle(root).routes
    assert bundle is not None
    return set(bundle.routes)


def _competing_change_is_not_blocked(
    base, root: Path, first_run: str, *, lose_acknowledgement: bool
) -> None:
    clock = MutableClock(NOW)
    acknowledgement = _BlockedAcknowledgement(lose_acknowledgement=lose_acknowledgement)
    service = _service(base, root, clock, acknowledgement)
    service.publish_run(first_run)
    slow_run = add_running_run(
        base, first_run, alias="slow", route_state="pending", identity=3
    )
    fast_run = add_running_run(
        base, first_run, alias="fast", route_state="pending", identity=4
    )

    acknowledgement.arm()
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(service.publish_run, slow_run)
        try:
            assert acknowledgement.entered.wait(timeout=30)
            # The slow publication is waiting on the supervisor right now.
            # A competing route change must neither queue behind its
            # transaction nor lose the route that already serves.
            competing = pool.submit(
                _service(base, root, clock, acknowledgement).publish_run, fast_run
            ).result(timeout=20)
            with base.sessions() as session:
                assert _recipe_run(session, first_run).route_state == "published"
                assert _recipe_run(session, fast_run).route_state == "published"
                assert _recipe_run(session, slow_run).route_state == "pending"
            assert {"qwen", "fast"} <= _live_aliases(root)
        finally:
            acknowledgement.release.set()
        observe_action(lambda: slow.result(timeout=60))

    assert competing.generation > 0
    assert _live_aliases(root) == {"qwen", "fast"}
    # Superseded is a wait, never a failed attempt: the worker re-reads.
    with base.sessions() as session:
        slow_row = _recipe_run(session, slow_run)
        assert slow_row.route_state == "pending"
        assert slow_row.route_attempts == 0
    service.publish_run(slow_run)
    assert {"qwen", "fast", "slow"} <= _live_aliases(root)
    with base.sessions() as session:
        assert {
            _recipe_run(session, run).route_state
            for run in (first_run, fast_run, slow_run)
        } == {"published"}


@pytest.mark.parametrize("lose_acknowledgement", [False, True])
def test_a_competing_route_change_is_not_blocked_by_a_slow_acknowledgement(
    tmp_path: Path,
    lose_acknowledgement: bool,
) -> None:
    base, _publisher, _applied, first_run = setup(tmp_path / "database")
    _competing_change_is_not_blocked(
        base, tmp_path / "live", first_run, lose_acknowledgement=lose_acknowledgement
    )


@pytest.mark.parametrize("lose_acknowledgement", [False, True])
def test_postgres_competing_route_change_is_not_blocked_by_a_slow_acknowledgement(
    tmp_path: Path, postgres_engine, lose_acknowledgement: bool
) -> None:
    base, _publisher, _applied, first_run = setup(
        tmp_path / "database", engine=postgres_engine
    )
    _competing_change_is_not_blocked(
        base, tmp_path / "live", first_run, lose_acknowledgement=lose_acknowledgement
    )


def test_postgres_slow_acknowledgement_outlasting_the_idle_timeout_publishes(
    tmp_path: Path, postgres_engine
) -> None:
    """The Controller's idle-in-transaction budget cannot cancel a publication."""

    engine: Engine = create_engine(
        postgres_engine.url,
        connect_args={"options": "-c idle_in_transaction_session_timeout=1000"},
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SHOW idle_in_transaction_session_timeout")
                ).scalar_one()
                == "1s"
            )
        clock = MutableClock(NOW)
        base, _publisher, _applied, run_id = setup(
            tmp_path / "database", clock=clock, engine=engine
        )
        root = tmp_path / "live"

        def slow_acknowledgement(_marker: ActivationMarker) -> None:
            time.sleep(2.5)

        service = _service(base, root, clock, slow_acknowledgement)
        generation = service.publish_run(run_id)

        marker = _verified_bundle(root).marker
        with base.sessions() as session:
            run = _recipe_run(session, run_id)
            assert run.route_state == "published"
            assert run.route_generation == generation.generation == marker.generation
            owner = _publication_owner(session)
            publication = _publication(session, owner.authority_id)
            assert publication.state == "completed"
            assert publication.activation_marker_digest == marker.digest
    finally:
        engine.dispose()


class _Crash(BaseException):
    """A process death: nothing in the Controller may catch it."""


def _crash_between_effect_and_completion_is_recovered(
    tmp_path: Path, engine: Engine | None
) -> None:
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(
        tmp_path / "database", clock=clock, engine=engine
    )
    root = tmp_path / "live"
    acknowledged: list[int] = []
    service = _service(
        base, root, clock, lambda marker: acknowledged.append(marker.generation)
    )

    def die(*_args, **_kwargs):
        raise _Crash

    service.projection_in_session = die  # type: ignore[method-assign]
    with base.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = "pending"
    try:
        service.publish_run(run_id)
    except _Crash:
        pass

    live = ActivationMarker.model_validate_json(
        AtomicRouteBundlePublisher(root).inspect().model_dump_json()
    )
    with base.sessions() as session:
        assert _recipe_run(session, run_id).route_state == "pending"

    restarted = _service(
        base, root, clock, lambda marker: acknowledged.append(marker.generation)
    )
    assert RecipeOperationWorker(base.sessions, restarted, clock=clock).tick() is True

    assert AtomicRouteBundlePublisher(root).inspect() == live
    with base.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == "published"
        assert run.route_generation == live.generation
        owner = _publication_owner(session)
        publication = _publication(session, owner.authority_id)
        assert publication.state == "completed"
        assert publication.generation == owner.owner_generation == live.generation
        claims = [
            row
            for row in session.query(RoutePublication)
            if row.authority_id != owner.authority_id
        ]
        assert all(row.state != "publication-pending" for row in claims)


def test_a_crash_between_effect_and_completion_is_recovered(tmp_path: Path) -> None:
    _crash_between_effect_and_completion_is_recovered(tmp_path, None)


def test_postgres_a_crash_between_effect_and_completion_is_recovered(
    tmp_path: Path, postgres_engine
) -> None:
    _crash_between_effect_and_completion_is_recovered(tmp_path, postgres_engine)


def test_damaged_plan_is_retryable_and_a_fresh_publication_is_admitted(tmp_path):
    """A damaged projection must not turn a recoverable publication into refusal."""
    from vonk_control.models import RecipeRun

    base, _publisher, _applied, run_id = setup(tmp_path)
    with base.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        plan = run.plan
        run.plan = {}
    observe_action(lambda: base.publish_run(run_id))
    with base.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.plan = plan
    assert base.publish_run(run_id).generation > 0


def test_disappeared_withdrawal_target_does_not_poison_completion(tmp_path):
    """Missing bookkeeping after activation cannot roll back its completion."""
    from vonk_control.models import RunNode

    base, _publisher, _applied, run_id = setup(tmp_path)
    base.publish_run(run_id)
    fresh_id = add_running_run(
        base, run_id, alias="fresh", route_state="withdrawn", identity=8
    )
    with base.publication_transaction() as session:
        withdrawal = base.prepare_withdrawal_in_session(session, frozenset({run_id}))
        publication = base._withdrawal_publication(session, withdrawal)
    with base.sessions.begin() as session:
        for node in session.query(RunNode).filter_by(run_id=run_id):
            session.delete(node)
        session.delete(_recipe_run(session, run_id))
    generation = base._execute(publication)
    assert generation.generation > 0
    assert base.publish_run(fresh_id).generation > generation.generation


@pytest.mark.parametrize("fault", [OSError, RuntimeError, ValueError])
def test_lost_acknowledgement_reconciles_the_same_generation(tmp_path, fault):
    """A lost ack must not require another request or activate another bundle."""
    base, _publisher, _applied, run_id = setup(tmp_path / "database")
    observed = []

    def acknowledge(marker):
        observed.append(marker.generation)
        if len(observed) == 1:
            raise fault("acknowledgement connection lost")

    service = _service(base, tmp_path / "live", lambda: NOW, acknowledge)
    generation = service.publish_run(run_id)
    assert observed == [generation.generation, generation.generation]
    assert _live_aliases(tmp_path / "live") == {"qwen"}


def test_ended_run_does_not_gate_a_fresh_operation(tmp_path):
    from vonk_agent_protocol import RunState

    from .observed_actions import observe_action

    service, _publisher, applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).state = RunState.FAILED
    observe_action(lambda: service.publish_run(run_id))
    assert not applied
    fresh_id = add_running_run(
        service, run_id, alias="fresh", route_state="pending", identity=9
    )
    service.publish_run(fresh_id)
    with service.sessions() as session:
        assert _recipe_run(session, fresh_id).route_state == "published"


def test_unknown_evidence_has_durable_backoff_and_recovers_without_a_new_request(
    tmp_path,
):
    """Unknown evidence must not spin on every tick or need manual publication."""
    from datetime import UTC, timedelta

    from vonk_agent_protocol import RouteState

    clock = MutableClock(NOW)
    service, _publisher, _applied, run_id = setup(tmp_path, clock=clock)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        plan = run.plan
        run.plan = {}
        run.route_state = RouteState.PENDING
    worker = RecipeOperationWorker(service.sessions, service, clock=clock)
    worker.tick()
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        due = run.route_next_attempt_at
        assert due is not None
        assert run.route_attempts == 1
    assert worker.tick() is False
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).plan = plan
    clock.now = due.replace(tzinfo=UTC) + timedelta(seconds=1)
    assert worker.tick() is True
    with service.sessions() as session:
        run = _recipe_run(session, run_id)
        assert run.route_state == RouteState.PUBLISHED
        assert run.route_next_attempt_at is None


@pytest.mark.parametrize(
    "fault", ["rank-gap", "mapping", "catalog", "endpoint", "stale"]
)
def test_bookkeeping_evidence_is_typed_temporary_and_repaired_in_place(tmp_path, fault):
    """Bookkeeping damage cannot become a permanent publication refusal."""
    from datetime import timedelta

    from vonk_control.models import (
        ClusterMapping,
        RecipeInstallation,
        RunNode,
    )

    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        node = session.query(RunNode).filter_by(run_id=run_id, rank=0).one()
        if fault == "rank-gap":
            row, field, old = node, "rank", node.rank
            node.rank = 10
        elif fault == "mapping":
            row = session.get(ClusterMapping, run.mapping_id)
            assert row is not None
            field, old = "endpoint_owner_node_id", row.endpoint_owner_node_id
            row.endpoint_owner_node_id = (
                session.query(RunNode).filter_by(run_id=run_id, rank=1).one().node_id
            )
        elif fault == "scheme":
            from vonk_control.recipe_execution_contract import parse_stored_run_endpoint

            endpoint = parse_stored_run_endpoint(node.endpoint)
            assert endpoint is not None
            node.endpoint = endpoint.model_copy(
                update={"url": endpoint.url.replace("http:", "https:")}
            ).model_dump(mode="json")
        elif fault == "catalog":
            row = session.get(RecipeInstallation, run.installation_id)
            assert row is not None
            field, old = "recipe_revision_id", row.recipe_revision_id
            row.recipe_revision_id = "00000000-0000-0000-0000-000000000000"
        elif fault == "endpoint":
            row, field, old = node, "endpoint", node.endpoint
            node.endpoint = None
        else:
            row, field, old = node, "updated_at", node.updated_at
            node.updated_at = NOW - timedelta(hours=1)
        model, key = type(row), session.identity_key(instance=row)[1]
    observe_action(lambda: service.publish_run(run_id))
    with service.sessions.begin() as session:
        healed = session.get(model, key)
        assert healed is not None
        setattr(healed, field, old)
    assert service.publish_run(run_id).generation > 0


def test_damaged_endpoint_projection_keeps_the_verified_serving_route(tmp_path):
    """Corrupt SQL evidence cannot revoke a checksum-verified working route."""
    from vonk_agent_protocol import RouteState
    from vonk_control.models import RunNode

    base, _publisher, _applied, run_id = setup(tmp_path / "database")
    service = _service(base, tmp_path / "live", lambda: NOW)
    service.publish_run(run_id)
    with service.sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=run_id, rank=0).one()
        node.endpoint = {}
    service.maintain()
    assert _live_aliases(tmp_path / "live") == {"qwen"}
    with service.sessions() as session:
        assert _recipe_run(session, run_id).route_state == RouteState.PUBLISHED


def test_publication_outage_exhausts_across_restart_without_stopping_workload(tmp_path):
    from datetime import UTC, timedelta

    from vonk_agent_protocol import RouteState, RunState

    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path, clock=clock)
    fresh = add_running_run(
        base, run_id, alias="fresh", route_state="withdrawn", identity=8
    )
    with base.sessions.begin() as session:
        _recipe_run(session, run_id).plan = {}
        _recipe_run(session, run_id).route_state = RouteState.PENDING
    for _attempt in range(6):
        RecipeOperationWorker(base.sessions, base, clock=clock).tick()
        with base.sessions() as session:
            row = _recipe_run(session, run_id)
            if row.route_next_attempt_at is not None:
                clock.now = row.route_next_attempt_at.replace(tzinfo=UTC) + timedelta(
                    seconds=1
                )
    with base.sessions() as session:
        row = _recipe_run(session, run_id)
        assert row.route_state != RouteState.PENDING
        assert row.state == RunState.RUNNING
        assert row.route_next_attempt_at is None
        assert row.plan == {}
    # No manual old-row repair is required for another accepted request.
    clock.now = NOW
    base.publish_run(fresh)
    with base.sessions() as session:
        assert _recipe_run(session, fresh).route_state == RouteState.PUBLISHED


def test_security_acknowledgement_is_never_retried_or_adopted(tmp_path):
    from vonk_agent_protocol import SecurityRefusalError, SecurityRefusalReason

    base, _publisher, _applied, run_id = setup(tmp_path)
    calls = []

    def denied(marker):
        calls.append(marker.digest)
        raise SecurityRefusalError(
            "denied acknowledgement", reason=SecurityRefusalReason.FORBIDDEN
        )

    service = _service(base, tmp_path / "live", lambda: NOW, denied)
    try:
        service.publish_run(run_id)
    except Exception as outcome:  # noqa: BLE001
        assert outcome is not None
    assert len(calls) == 1
    with base.sessions() as session:
        assert _recipe_run(session, run_id).route_state != "published"
    _service(base, tmp_path / "live", lambda: NOW).publish_run(run_id)
    assert _live_aliases(tmp_path / "live") == {"qwen"}


def _stale_executor_is_fenced_before_first_activation(
    tmp_path, monkeypatch, engine=None
):
    from contextlib import contextmanager

    base, _publisher, _applied, first = setup(tmp_path, engine=engine)
    slow_id = add_running_run(
        base, first, alias="slow", route_state="pending", identity=3
    )
    fast_id = add_running_run(
        base, first, alias="fast", route_state="pending", identity=4
    )
    root = tmp_path / "live"
    service = _service(base, root, lambda: NOW)
    service.publish_run(first)
    entered = threading.Event()
    release = threading.Event()
    original = service._publisher._publisher._locked
    slow_thread = []

    @contextmanager
    def paused_lock():
        if threading.get_ident() in slow_thread:
            entered.set()
            assert release.wait(60)
        with original() as result:
            yield result

    monkeypatch.setattr(service._publisher._publisher, "_locked", paused_lock)

    def slow():
        slow_thread.append(threading.get_ident())
        try:
            service.publish_run(slow_id)
        except Exception:  # noqa: BLE001
            return

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(slow)
        try:
            assert entered.wait(60)
            service.publish_run(fast_id)
            accepted = _verified_bundle(root).marker.digest
        finally:
            release.set()
        future.result(60)
    assert _verified_bundle(root).marker.digest == accepted
    assert _live_aliases(root) == {"qwen", "fast"}
    service.publish_run(slow_id)
    assert _live_aliases(root) == {"qwen", "fast", "slow"}


def test_stale_executor_is_fenced_before_first_activation(tmp_path, monkeypatch):
    _stale_executor_is_fenced_before_first_activation(tmp_path, monkeypatch)


def test_postgres_stale_executor_is_fenced_before_first_activation(
    tmp_path, monkeypatch, postgres_engine
):
    _stale_executor_is_fenced_before_first_activation(
        tmp_path, monkeypatch, postgres_engine
    )


def test_target_change_during_activation_owes_cleanup_across_restart(tmp_path):
    from vonk_agent_protocol import RoutePublicationState, RunState
    from vonk_control.models import RecipeRun
    from vonk_control.route_runtime import RECIPE_ROUTE_AUTHORITY_ID

    from .observed_actions import observe_action

    base, _publisher, _applied, first = setup(tmp_path)
    target = add_running_run(
        base, first, alias="target", route_state="pending", identity=3
    )
    root = tmp_path / "live"
    clock = MutableClock(NOW)
    service = _service(base, root, clock)
    service.publish_run(first)
    changed = [False]
    broken = [False]

    def acknowledge(_marker):
        if not changed[0]:
            with base.sessions.begin() as session:
                row = session.get(RecipeRun, target)
                assert row is not None
                row.state = RunState.STOPPED
            changed[0] = True
        elif broken[0]:
            raise OSError("cleanup acknowledgement unavailable")

    changing = _service(base, root, clock, acknowledge)
    changing.publish_run(target)
    with base.sessions() as session:
        assert (
            _publication(session, RECIPE_ROUTE_AUTHORITY_ID).state
            == RoutePublicationState.WITHDRAWAL_PENDING
        )
    assert _live_aliases(root) == {"qwen", "target"}
    broken[0] = True
    observe_action(changing.maintain)
    with base.sessions() as session:
        owner = _publication_owner(session)
        due = owner.reconciliation_next_at
        assert due is not None
    # The failed observation ends; the durable desired cleanup survives it.
    from datetime import UTC, timedelta

    clock.now = due.replace(tzinfo=UTC) + timedelta(seconds=1)
    restarted = _service(base, root, clock)
    restarted.maintain()
    assert _live_aliases(root) == {"qwen"}
    fresh = add_running_run(
        base, first, alias="fresh", route_state="pending", identity=4
    )
    clock.now = NOW
    restarted.publish_run(fresh)
    assert _live_aliases(root) == {"qwen", "fresh"}


@pytest.mark.parametrize(
    "damage",
    ["digest", "identity", "port", "scheme", "retired-catalog", "history", "marker"],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_owner_scoped_damage_never_blocks_an_unrelated_publication(tmp_path, damage):
    from sqlalchemy import select, update
    from vonk_agent_protocol import LifecycleState
    from vonk_control.models import (
        CatalogDocumentRevision,
        Job,
        RecipeInstallation,
        RunNode,
    )

    base, _publisher, _applied, first = setup(tmp_path)
    second = add_running_run(
        base, first, alias="second", route_state="pending", identity=4
    )
    root = tmp_path / "live"
    service = _service(base, root, lambda: NOW)
    service.publish_run(first)
    with base.sessions.begin() as session:
        run = _recipe_run(session, first)
        node = session.scalar(
            select(RunNode).where(RunNode.run_id == first, RunNode.rank == 0)
        )
        assert node is not None
        if damage == "digest":
            session.connection().exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            run.plan_digest = ""
        elif damage == "identity":
            session.connection().exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            node.role = ""
        elif damage == "port":
            node.port += 1
        elif damage == "scheme":
            from vonk_control.recipe_execution_contract import parse_stored_run_endpoint

            endpoint = parse_stored_run_endpoint(node.endpoint)
            assert endpoint is not None
            node.endpoint = endpoint.model_copy(
                update={"url": endpoint.url.replace("http:", "https:")}
            ).model_dump(mode="json")
        elif damage == "retired-catalog":
            installation = session.get(RecipeInstallation, run.installation_id)
            assert installation is not None
            revision = session.get(
                CatalogDocumentRevision, installation.recipe_revision_id
            )
            assert revision is not None
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(state=LifecycleState.FAILED.value)
            )
        else:
            session.add(
                Job(
                    kind="recipe.start",
                    request_id="70000000-0000-4000-8000-000000000007",
                    actor="admin",
                    authority_revision="test-authority",
                    targets=[],
                    payload_digest="a" * 64,
                    payload=None,
                    state=LifecycleState.FAILED.value,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    if damage == "marker":
        (root / "activation.json").write_bytes(b"{")
    service.publish_run(second)
    assert _live_aliases(root) == {"qwen", "second"}
    service.withdraw_run(second)
    assert _live_aliases(root) == {"qwen"}


def test_authenticated_observation_producer_repairs_between_request_attempts(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    from sqlalchemy import select
    from vonk_control.models import AgentCertificate, RunNode
    from vonk_control.recipe_routes import service as service_module

    from .test_agent_api import agent_headers, make_agent_system

    base, _publisher, applied, run_id = setup(tmp_path, ranks=1)
    observer_root = tmp_path / "observer"
    observer_root.mkdir()
    client, services, _codec, agent_clock = make_agent_system(
        observer_root, engine=base.sessions.kw["bind"]
    )
    agent_clock.now = NOW
    with base.sessions.begin() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        node_id = node.node_id
        node.observed_run_generation = None
        node.observation_observed_at = None
        session.add(
            AgentCertificate(
                serial="route-serial",
                node_id=node_id,
                fingerprint="fingerprint-route-serial",
                not_before=NOW - timedelta(seconds=1),
                not_after=NOW + timedelta(hours=1),
            )
        )

    attempts = []

    def observations():
        attempts.append(0)
        yield 0
        # The real observation writer must be able to commit here: the failed
        # request observation has rolled back and released its owner/rank locks.
        response = client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(node_id, "route-serial"),
            json={
                "observed_at": NOW.isoformat(),
                "runs": [
                    {
                        "run_id": run_id,
                        "run_generation": 1,
                        "process_running": True,
                        "endpoint_ready": True,
                    }
                ],
            },
        )
        assert response.status_code == 204, response.text
        attempts.append(1)
        yield 1

    monkeypatch.setattr(service_module, "bounded_attempts", observations)
    base.publish_run(run_id)
    assert attempts[:2] == [0, 1]
    assert len(applied) == 1
    with services.sessions() as session:
        assert _recipe_run(session, run_id).route_state == "published"


@pytest.mark.usefixtures("damaged_json_rows")
def test_corrupt_owned_recovery_metadata_ends_only_its_attempt_and_allows_fresh(
    tmp_path,
):
    from datetime import UTC, timedelta

    from vonk_agent_protocol import AgentOperation, LifecycleState, RunState
    from vonk_control.models import Job
    from vonk_control.recipe_operation_worker import RecipeOperationWorker

    base, _publisher, _applied, first = setup(tmp_path)
    fresh = add_running_run(
        base, first, alias="fresh", route_state="withdrawn", identity=4
    )
    with base.sessions.begin() as session:
        _recipe_run(session, first).route_state = "pending"
        job = Job(
            kind=AgentOperation.RECIPE_START,
            request_id="70000000-0000-4000-8000-000000000008",
            actor="admin",
            authority_revision="test-authority",
            targets=[],
            payload_digest="a" * 64,
            payload={"owner_id": first, "recovery": {"deadline": "damaged"}},
            state=LifecycleState.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(job)
        session.flush()
        identity = job.id
    clock = MutableClock(NOW)
    service = _service(base, tmp_path / "live", clock)
    for _attempt in range(6):
        RecipeOperationWorker(base.sessions, service, clock=clock).tick()
        with base.sessions() as session:
            row = _recipe_run(session, first)
            if row.route_next_attempt_at is not None:
                clock.now = row.route_next_attempt_at.replace(tzinfo=UTC) + timedelta(
                    seconds=1
                )
    with base.sessions() as session:
        ended = session.get(Job, identity)
        assert ended is not None
        assert ended.state != LifecycleState.RUNNING
        assert _recipe_run(session, first).state == RunState.RUNNING
        assert _recipe_run(session, first).route_next_attempt_at is None
    clock.now = NOW
    service.publish_run(fresh)
    assert _live_aliases(tmp_path / "live") == {"fresh", "qwen"}


@pytest.mark.usefixtures("damaged_json_rows")
def test_candidate_outage_ends_at_original_recovery_deadline_and_fresh_publishes(
    tmp_path,
):
    """Backoff cannot postpone settlement past the accepted recovery deadline."""
    from datetime import UTC, timedelta

    from vonk_agent_protocol import AgentOperation, LifecycleState, RouteState, RunState
    from vonk_control.job_documents import DistributedRecoveryMarker
    from vonk_control.models import Job

    clock = MutableClock(NOW)
    base, _publisher, _applied, first = setup(tmp_path, clock=clock)
    fresh = add_running_run(
        base, first, alias="fresh", route_state=RouteState.WITHDRAWN, identity=4
    )
    deadline = NOW + timedelta(seconds=1)
    with base.sessions.begin() as session:
        run = _recipe_run(session, first)
        good_plan = run.plan
        run.plan = {}
        run.route_state = RouteState.PENDING
        job = Job(
            kind=AgentOperation.RECIPE_START,
            request_id="70000000-0000-4000-8000-000000000009",
            actor="admin",
            authority_revision="test-authority",
            targets=[],
            payload_digest="a" * 64,
            payload={
                "owner_id": first,
                "recovery": DistributedRecoveryMarker(
                    schema_version=1, failed_rank=1, deadline=deadline.isoformat()
                ).model_dump(mode="json", exclude_none=True),
            },
            state=LifecycleState.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(job)
        session.flush()
        job_id = job.id
    root = tmp_path / "live"
    service = _service(base, root, clock)
    worker = RecipeOperationWorker(base.sessions, service, clock=clock)
    worker.tick()
    with base.sessions() as session:
        due = _recipe_run(session, first).route_next_attempt_at
        assert due is not None and due.replace(tzinfo=UTC) <= deadline
    clock.now = deadline
    RecipeOperationWorker(base.sessions, service, clock=clock).tick()
    with base.sessions() as session:
        ended = session.get(Job, job_id)
        assert ended is not None and ended.state == LifecycleState.FAILED
        run = _recipe_run(session, first)
        assert run.state == RunState.RUNNING
        assert run.route_next_attempt_at is None
    with base.sessions.begin() as session:
        _recipe_run(session, first).plan = good_plan
    service.publish_run(fresh)
    assert _live_aliases(root) == {"qwen", "fresh"}
