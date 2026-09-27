from __future__ import annotations

import threading
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import Base
from vonk_control.recipe_operation_worker import RecipeOperationWorker


def test_route_leases_renew_while_a_coordinator_is_blocked(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'route-worker.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    renewal_started = threading.Event()
    coordinator_started = threading.Event()
    release_coordinator = threading.Event()

    class Routes:
        def maintain(self, *, renew_before_seconds: int = 10) -> bool:
            renewal_started.set()
            return False

        def publish_run(self, run_id: str) -> object:
            raise AssertionError("no pending route should be published")

    def slow_cleanup() -> bool:
        coordinator_started.set()
        assert release_coordinator.wait(5)
        return False

    worker = RecipeOperationWorker(
        sessions,
        Routes(),
        clock=lambda: datetime.now(UTC),
        build_cleanup=slow_cleanup,
        manage_route_leases_in_background=True,
    )
    tick = threading.Thread(target=worker.tick)
    try:
        tick.start()
        assert coordinator_started.wait(1)
        assert renewal_started.wait(1)
    finally:
        release_coordinator.set()
        tick.join(timeout=2)
        worker.close()

    assert not tick.is_alive()
    engine.dispose()
