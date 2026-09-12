from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.jobs import JobService
from vonk_control.models import Base
from vonk_control.worker import HandlerRequest, Worker


def _service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    return JobService(sessionmaker(engine, expire_on_commit=False), clock=lambda: datetime(2026, 8, 3, tzinfo=UTC))


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
def test_generic_worker_leaves_unregistered_jobs_for_their_owner(tmp_path, kind) -> None:
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


def test_worker_does_not_mask_unexpected_programming_error(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "admin", "abc", [], {})

    with pytest.raises(AssertionError, match="programming defect"):
        Worker(
            jobs,
            "worker-1",
            {"probe": lambda _request: (_ for _ in ()).throw(
                AssertionError("programming defect")
            )},
        ).run_once()


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
