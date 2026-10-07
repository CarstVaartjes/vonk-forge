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
