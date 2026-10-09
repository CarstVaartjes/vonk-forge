import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control import job_states, telemetry_maintenance
from vonk_control.jobs import JobService
from vonk_control.models import Base
from vonk_control.worker import HandlerRequest, Worker, WorkerWatchdog


def _service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    return JobService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )


def test_shutdown_retries_unknown_closer_and_continues_other_services(
    tmp_path, monkeypatch
):
    """Catches abandoning remaining executors after one checkpoint is busy."""
    from vonk_agent_protocol import UnknownOutcomeError, WaitReason
    from vonk_control import worker as module

    calls = []

    def busy():
        calls.append("busy")
        raise UnknownOutcomeError(
            "checkpoint unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
        )

    monkeypatch.setattr(module, "bounded_attempts", lambda: iter(range(3)))
    worker = Worker(
        _service(tmp_path),
        "worker",
        {},
        background_closers=(busy, lambda: calls.append("closed")),
    )
    worker.close()
    worker.close()
    assert calls == ["busy", "busy", "busy", "closed"]


def test_worker_dispatches_registered_handler_and_persists_result(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("probe", "admin", "abc", ["node"], {"value": 4})

    def probe(payload: HandlerRequest) -> dict[str, object]:
        value = payload["value"]
        assert isinstance(value, int)
        return {"result": value + 1}

    worker = Worker(jobs, "worker-1", {"probe": probe})
    assert worker.run_once()
    assert jobs.get(job.id).state == "succeeded"
    assert jobs.get(job.id).result == {"result": 5}


def test_worker_handler_receives_pinned_job_metadata(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("probe", "admin", "a" * 64, ["spk_a"], {"value": 4})
    received = []

    def handle(request: HandlerRequest):
        received.append(request)
        return {"ok": True}

    Worker(jobs, "worker-a", {"probe": handle}).run_once()

    assert received[0]["value"] == 4
    assert received[0].authority_revision == "a" * 64
    assert received[0].targets == ("spk_a",)
    assert jobs.get(job.id).state == "succeeded"


@pytest.mark.parametrize("kind", ["recipe.image.availability.v2", "future-coordinator"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_generic_worker_leaves_unregistered_jobs_for_their_owner(
    tmp_path, kind
) -> None:
    jobs = _service(tmp_path)
    pending = jobs.enqueue(kind, "admin", "abc", [], {"retry_after_at": "later"})
    assert Worker(jobs, "worker-1", {}).run_once() is False
    generic = jobs.enqueue("probe", "admin", "abc", [], {})
    worker = Worker(jobs, "worker-1", {"probe": lambda _: {"done": True}})
    assert worker.run_once()
    assert jobs.get(generic.id).state == "succeeded"
    assert worker.run_once() is False
    stored = jobs.get(pending.id)
    assert stored.state == "queued"
    assert stored.current_attempt == 0
    assert stored.payload == {"retry_after_at": "later"}


def test_worker_handler_fault_ends_locally_and_a_fresh_job_is_admitted(
    tmp_path,
) -> None:
    """Catches a programming fault abandoning its claim and the whole turn."""
    jobs = _service(tmp_path)
    damaged = jobs.enqueue("probe", "admin", "abc", [], {})
    worker = Worker(
        jobs,
        "worker-1",
        {
            "probe": lambda _request: (_ for _ in ()).throw(
                AssertionError("programming defect")
            )
        },
    )
    assert worker.run_once()
    assert jobs.get(damaged.id).state in job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    worker = Worker(jobs, "worker-1", {"healthy": lambda _: {"done": True}})
    assert worker.run_once()
    assert jobs.get(fresh.id).result == {"done": True}


def test_worker_runs_route_housekeeping_even_when_queue_is_idle(tmp_path) -> None:
    jobs = _service(tmp_path)
    calls = []

    worker = Worker(
        jobs,
        "worker-1",
        {},
        housekeeping=lambda: calls.append("refresh"),
    )

    assert worker.run_once() is False
    assert calls == ["refresh"]


def test_worker_heartbeat_runs_after_idle_housekeeping(tmp_path) -> None:
    jobs = _service(tmp_path)
    calls = []
    worker = Worker(
        jobs,
        "worker-1",
        {},
        housekeeping=lambda: calls.append("housekeeping"),
        loop_heartbeat=lambda: calls.append("heartbeat"),
    )

    assert worker.run_once() is False
    assert calls == ["housekeeping", "heartbeat"]


def test_worker_contains_a_database_wait_timeout_and_still_heartbeats(
    tmp_path, caplog
) -> None:
    jobs = _service(tmp_path)
    calls = []

    def contended() -> bool:
        raise OperationalError("SELECT 1", {}, Exception("canceling statement"))

    worker = Worker(
        jobs,
        "worker-1",
        {},
        loop_heartbeat=lambda: calls.append("heartbeat"),
        background_services=(contended,),
    )

    with caplog.at_level("INFO", logger="vonk-control-worker"):
        assert worker.run_once() is False

    # A bounded database wait is a dependency failure, not a tick failure: the
    # loop still heartbeats and the failing source is reported for retry.
    assert calls == ["heartbeat"]
    assert "worker.source_failed" in caplog.text


def test_worker_source_failure_logs_redacted_message_and_traceback(
    tmp_path, caplog
) -> None:
    worker = Worker(
        _service(tmp_path),
        "worker-1",
        {},
        background_services=(
            lambda: (_ for _ in ()).throw(RuntimeError("Bearer secret-value")),
        ),
    )

    with caplog.at_level("INFO", logger="vonk-control-worker"):
        worker.run_once()

    assert "Traceback" in caplog.text
    assert "secret-value" not in caplog.text
    assert "<redacted>" in caplog.text


def test_worker_watchdog_detects_stall_and_resets_after_loop() -> None:
    now = [10.0]
    watchdog = WorkerWatchdog(timeout_seconds=30, clock=lambda: now[0])

    now[0] = 40.1
    assert watchdog.stalled()
    watchdog.beat()
    assert not watchdog.stalled()


def test_worker_ticks_recipe_operations_before_generic_jobs(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class RecipeOperations:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> bool:
            self.calls += 1
            return True

    recipes = RecipeOperations()
    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request) or {}},
        recipes=recipes,
    )

    assert worker.run_once() is True
    assert recipes.calls == 1
    assert handled == []


def test_worker_falls_through_when_recipe_operations_are_idle(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class RecipeOperations:
        def tick(self) -> bool:
            return False

    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request.kind) or {}},
        recipes=RecipeOperations(),
    )

    assert worker.run_once() is True
    assert handled == ["probe"]


def test_worker_alternates_busy_recipe_and_generic_job_queues(
    tmp_path,
) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class RecipeOperations:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> bool:
            self.calls += 1
            return True

    recipes = RecipeOperations()
    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request.kind) or {}},
        recipes=recipes,
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert recipes.calls == 1
    assert handled == ["probe"]


def test_worker_alternates_recipe_and_generic_without_starvation(
    tmp_path,
) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {"index": 1})
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {"index": 2})
    events: list[str] = []

    class Source:
        def __init__(self, name: str) -> None:
            self.name = name

        def tick(self) -> bool:
            events.append(self.name)
            return True

    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda _request: events.append("generic") or {}},
        recipes=Source("recipe"),
    )

    assert [worker.run_once() for _ in range(4)] == [True] * 4
    assert events == [
        "recipe",
        "generic",
        "recipe",
        "generic",
    ]


def test_telemetry_maintenance_cadence_is_fixed_aware_and_does_not_burst() -> None:
    current = datetime(2026, 8, 15, 12, tzinfo=UTC)
    calls: list[datetime] = []

    class Maintenance(telemetry_maintenance.TelemetryMaintenance):
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run_once(self, *args: object, **kwargs: object) -> None:
            calls.append(current)

    cadence = telemetry_maintenance.TelemetryMaintenanceCadence(
        Maintenance(), clock=lambda: current
    )

    cadence()
    cadence()
    assert calls == [datetime(2026, 8, 15, 12, tzinfo=UTC)]

    current += timedelta(seconds=14, microseconds=999999)
    cadence()
    assert len(calls) == 1

    current += timedelta(microseconds=1)
    cadence()
    assert len(calls) == 2

    current += timedelta(seconds=45)
    cadence()
    cadence()
    assert len(calls) == 3

    invalid = telemetry_maintenance.TelemetryMaintenanceCadence(
        Maintenance(), clock=lambda: current.replace(tzinfo=None)
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        invalid()


def test_due_telemetry_housekeeping_does_not_consume_worker_source_turn(
    tmp_path,
) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})
    current = datetime(2026, 8, 15, 12, tzinfo=UTC)
    events: list[str] = []

    class Maintenance(telemetry_maintenance.TelemetryMaintenance):
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run_once(self, *args: object, **kwargs: object) -> None:
            events.append("maintenance")

    class Source:
        def __init__(self, name: str) -> None:
            self.name = name

        def tick(self) -> bool:
            events.append(self.name)
            return True

    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda _request: events.append("generic") or {}},
        housekeeping=telemetry_maintenance.TelemetryMaintenanceCadence(
            Maintenance(), clock=lambda: current
        ),
        recipes=Source("recipe"),
    )

    for _ in range(3):
        assert worker.run_once() is True
        current += timedelta(seconds=15)

    assert events == [
        "maintenance",
        "recipe",
        "maintenance",
        "generic",
        "maintenance",
        "recipe",
    ]


def test_failing_source_does_not_starve_a_healthy_durable_job(tmp_path, caplog) -> None:
    """A source that keeps failing must not deny unrelated jobs their turn."""

    jobs = _service(tmp_path)
    for index in range(3):
        jobs.enqueue("probe", "admin", "a" * 64, ["node"], {"index": index})

    class RecoveringSource:
        def __init__(self) -> None:
            self.calls = 0
            self.available = False

        def tick(self) -> bool:
            self.calls += 1
            if not self.available:
                raise RuntimeError("dependency unavailable")
            return True

    recipes = RecoveringSource()
    handled: list[int] = []

    def handle(request: HandlerRequest) -> dict[str, object]:
        index = request["index"]
        assert isinstance(index, int)
        handled.append(index)
        return {}

    worker = Worker(
        jobs,
        "worker-1",
        {"probe": handle},
        recipes=recipes,
    )

    with caplog.at_level(logging.INFO, logger="vonk-control-worker"):
        # The failing source is first in rotation every pass because the
        # durable job source advances the cursor back to it.  The job must
        # still complete on each pass.
        assert [worker.run_once() for _ in range(3)] == [True, True, True]
        assert sorted(handled) == [0, 1, 2]
        assert recipes.calls == 3
        assert "worker.source_failed" in caplog.text
        # Once the dependency recovers, the previously failing source resumes.
        recipes.available = True
        assert worker.run_once() is True
        assert recipes.calls == 4
        assert sorted(handled) == [0, 1, 2]


def test_failing_housekeeping_task_does_not_stop_sources_or_heartbeat(
    tmp_path, caplog
) -> None:
    """One failing maintenance task must not take the whole pass down."""

    jobs = _service(tmp_path)
    jobs.enqueue("probe", "admin", "a" * 64, ["node"], {})
    events: list[str] = []

    def failing_housekeeping() -> None:
        events.append("housekeeping")
        raise RuntimeError("maintenance dependency unavailable")

    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda _request: events.append("job") or {}},
        housekeeping=failing_housekeeping,
        loop_heartbeat=lambda: events.append("heartbeat"),
    )

    with caplog.at_level(logging.INFO, logger="vonk-control-worker"):
        assert worker.run_once() is True

    assert events == ["housekeeping", "job", "heartbeat"]
    assert "worker.housekeeping_failed" in caplog.text


@pytest.mark.parametrize("owner", ["recipe", "housekeeping", "background"])
def test_unexpected_owner_fault_does_not_suppress_other_work(
    tmp_path, owner, monkeypatch
):
    """Catches only guarding a fixed list of operational exception types."""
    calls = []
    broken = True
    now = [10.0]
    monkeypatch.setattr("vonk_control.worker.time.monotonic", lambda: now[0])

    def task():
        calls.append(owner)
        if broken:
            raise AssertionError("owner defect")
        return False

    class Recipes:
        tick = staticmethod(task)

    jobs = _service(tmp_path)
    first = jobs.enqueue("healthy", "admin", "abc", [], {})
    worker = Worker(
        jobs,
        "worker",
        {"healthy": lambda _: {"done": True}},
        recipes=Recipes() if owner == "recipe" else None,
        housekeeping=task if owner == "housekeeping" else None,
        background_services=(task,) if owner == "background" else (),
    )
    assert worker.run_once()
    assert jobs.get(first.id).result == {"done": True}
    broken = False
    now[0] += 5
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    assert worker.run_once()
    assert jobs.get(fresh.id).result == {"done": True}
    assert calls == [owner, owner]


def test_unavailable_model_cache_surface_is_local_and_fresh_work_observes_repair(
    tmp_path, monkeypatch
):
    """Catches missing adapter bookkeeping preventing unrelated/new dispatch."""

    class Cache:
        pass

    now = [10.0]
    calls = []
    monkeypatch.setattr("vonk_control.worker.time.monotonic", lambda: now[0])
    jobs = _service(tmp_path)
    first = jobs.enqueue("healthy", "admin", "abc", [], {})
    worker = Worker(
        jobs, "worker", {"healthy": lambda _: {"done": True}}, model_cache=Cache()
    )
    assert worker.run_once()
    assert jobs.get(first.id).result == {"done": True}

    def tick(_self):
        calls.append("cache")
        return False

    monkeypatch.setattr(Cache, "tick", tick, raising=False)
    now[0] += 5
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    assert worker.run_once()
    assert jobs.get(fresh.id).result == {"done": True}
    assert calls == ["cache"]


def test_stalled_clock_sample_does_not_block_watchdog_observation():
    """Catches a clock callback holding the lock the watchdog itself needs."""
    now = [0.0]
    entered = threading.Event()
    released = threading.Event()
    main_thread = threading.get_ident()

    def clock():
        if threading.get_ident() != main_thread:
            entered.set()
            released.wait(2)
        return now[0]

    watchdog = WorkerWatchdog(timeout_seconds=30, clock=clock)
    sampling = threading.Thread(target=watchdog.beat)
    sampling.start()
    try:
        assert entered.wait(2)
        now[0] = 31.0
        assert watchdog.stalled()
    finally:
        released.set()
        sampling.join(2)
    assert not sampling.is_alive()
    assert not watchdog.stalled()
    now[0] += 31
    assert watchdog.stalled()
    watchdog.beat()
    assert not watchdog.stalled()


@pytest.mark.parametrize(
    "fault", [OSError("checkpoint unavailable"), AssertionError("owner defect")]
)
def test_shutdown_fault_is_bounded_and_other_owners_and_fresh_work_continue(
    tmp_path, monkeypatch, fault
):
    """Catches unexpected closer errors suppressing every later shutdown owner."""
    from vonk_control import worker as module

    calls = []

    def broken():
        calls.append("broken")
        raise fault

    monkeypatch.setattr(module, "bounded_attempts", lambda: iter(range(3)))
    jobs = _service(tmp_path)
    Worker(
        jobs, "old", {}, background_closers=(broken, lambda: calls.append("closed"))
    ).close()
    assert calls == ["broken", "broken", "broken", "closed"]
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    assert Worker(jobs, "new", {"healthy": lambda _: {"done": True}}).run_once()
    assert jobs.get(fresh.id).result == {"done": True}
