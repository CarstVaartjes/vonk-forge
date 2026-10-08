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
    RecipeRouteNotReady,
    RecipeRouteService,
    RecipeRouteSuperseded,
    publication_is_temporary,
)
from vonk_control.route_runtime import (
    ActivationMarker,
    AtomicRouteBundlePublisher,
    verify_active_route_bundle,
)

from .test_recipe_routes import (
    NOW,
    MutableClock,
    _publication,
    _publication_owner,
    _recipe_run,
    add_running_run,
    setup,
)


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

    def __init__(self) -> None:
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


def _live_aliases(root: Path) -> set[str]:
    bundle = verify_active_route_bundle(root).routes
    assert bundle is not None
    return set(bundle.routes)


def _competing_change_is_not_blocked(base, root: Path, first_run: str) -> None:
    clock = MutableClock(NOW)
    acknowledgement = _BlockedAcknowledgement()
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
        with pytest.raises(RecipeRouteSuperseded) as superseded:
            slow.result(timeout=60)

    assert competing.generation > 0
    # Superseded is a wait, never a failed attempt: the worker re-reads.
    assert isinstance(superseded.value, RecipeRouteNotReady)
    assert publication_is_temporary(superseded.value)
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


def test_a_competing_route_change_is_not_blocked_by_a_slow_acknowledgement(
    tmp_path: Path,
) -> None:
    base, _publisher, _applied, first_run = setup(tmp_path / "database")
    _competing_change_is_not_blocked(base, tmp_path / "live", first_run)


def test_postgres_competing_route_change_is_not_blocked_by_a_slow_acknowledgement(
    tmp_path: Path, postgres_engine
) -> None:
    base, _publisher, _applied, first_run = setup(
        tmp_path / "database", engine=postgres_engine
    )
    _competing_change_is_not_blocked(base, tmp_path / "live", first_run)


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

        marker = verify_active_route_bundle(root).marker
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
    with pytest.raises(_Crash):
        service.publish_run(run_id)

    live = AtomicRouteBundlePublisher(root).inspect()
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
    from vonk_agent_protocol import UnknownOutcomeError
    from vonk_control.models import RecipeRun

    base, _publisher, _applied, run_id = setup(tmp_path)
    with base.sessions.begin() as session:
        run = _recipe_run(session, run_id)
        plan = run.plan
        run.plan = {}
    with pytest.raises(RecipeRouteNotReady) as caught:
        base.publish_run(run_id)
    assert isinstance(caught.value, UnknownOutcomeError)
    assert caught.value.typed_error() is not None
    assert publication_is_temporary(caught.value)
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
        base, run_id, alias="fresh", route_state="pending", identity=8
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


def test_lost_acknowledgement_reconciles_the_same_generation(tmp_path):
    """A lost ack must not require another request or activate another bundle."""
    base, _publisher, _applied, run_id = setup(tmp_path / "database")
    observed = []

    def acknowledge(marker):
        observed.append(marker.generation)
        if len(observed) == 1:
            raise OSError("acknowledgement connection lost")

    service = _service(base, tmp_path / "live", lambda: NOW, acknowledge)
    generation = service.publish_run(run_id)
    assert observed == [generation.generation, generation.generation]
    assert _live_aliases(tmp_path / "live") == {"qwen"}


def test_ended_run_has_a_typed_end_and_a_fresh_operation_is_admitted(tmp_path):
    """Ended targets cannot leave publication claims blocking a new run."""
    from vonk_agent_protocol import RunState, WaitReason

    from .non_blocking import assert_ended_without_blocking

    service, _publisher, _applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        original = _recipe_run(session, run_id)
        original.state = RunState.FAILED

    reasons: list[WaitReason] = []

    def end(run):
        with pytest.raises(RecipeRouteSuperseded) as caught:
            service.publish_run(run.id)
        assert caught.value.typed_reason == WaitReason.SCOPE_CHANGED
        reasons.append(caught.value.typed_reason)
        return run

    def fresh(world):
        fresh_id = add_running_run(
            world, run_id, alias="fresh", route_state="pending", identity=9
        )
        world.publish_run(fresh_id)
        with world.sessions() as session:
            return _recipe_run(session, fresh_id)

    def assert_reason(_run):
        assert reasons == [WaitReason.SCOPE_CHANGED]

    assert_ended_without_blocking(
        service,
        original,
        end=end,
        fresh=fresh,
        request_key=lambda run: run.id,
        assert_reason=assert_reason,
    )


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

    from vonk_agent_protocol import UnknownOutcomeError
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
    with pytest.raises(RecipeRouteNotReady) as caught:
        service.publish_run(run_id)
    assert isinstance(caught.value, UnknownOutcomeError)
    assert caught.value.typed_error() is not None
    assert publication_is_temporary(caught.value)
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
