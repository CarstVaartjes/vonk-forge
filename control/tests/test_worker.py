from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
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
    worker = Worker(jobs, "worker-1", {"probe": lambda payload: {"result": payload["value"] + 1}})
    assert worker.run_once()
    assert jobs.get(job.id).state == "succeeded"
    assert jobs.get(job.id).result == {"result": 5}


def test_worker_handler_receives_pinned_job_metadata(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("probe", "admin", "a" * 40, ["spk_a"], {"value": 4})
    received = []

    def handle(request: HandlerRequest):
        received.append(request)
        return {"ok": True}

    Worker(jobs, "worker-a", {"probe": handle}).run_once()

    assert received[0]["value"] == 4
    assert received[0].base_commit == "a" * 40
    assert received[0].targets == ("spk_a",)
    assert jobs.get(job.id).state == "succeeded"


def test_unknown_job_kind_fails_without_execution(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("unknown", "admin", "abc", [], {})
    assert Worker(jobs, "worker-1", {}).run_once()
    assert jobs.get(job.id).state == "failed"


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


def test_worker_ticks_durable_reconciliations_before_generic_jobs(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class Reconciliations:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> bool:
            self.calls += 1
            return True

    reconciliations = Reconciliations()
    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request) or {}},
        reconciliations=reconciliations,
    )

    assert worker.run_once() is True
    assert reconciliations.calls == 1
    assert handled == []


def test_worker_falls_through_when_no_reconciliation_can_advance(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class Reconciliations:
        def tick(self) -> bool:
            return False

    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request.kind) or {}},
        reconciliations=Reconciliations(),
    )

    assert worker.run_once() is True
    assert handled == ["probe"]


def test_worker_alternates_busy_reconciliation_and_generic_job_queues(
    tmp_path,
) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class Reconciliations:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> bool:
            self.calls += 1
            return True

    reconciliations = Reconciliations()
    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request.kind) or {}},
        reconciliations=reconciliations,
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert reconciliations.calls == 1
    assert handled == ["probe"]


def test_worker_round_robins_reconciliation_update_and_generic_without_starvation(
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
        reconciliations=Source("reconciliation"),
        updates=Source("update"),
    )

    assert [worker.run_once() for _ in range(6)] == [True] * 6
    assert events == [
        "reconciliation",
        "update",
        "generic",
        "reconciliation",
        "update",
        "generic",
    ]


def test_worker_round_robins_package_rollouts_with_other_durable_sources(tmp_path) -> None:
    jobs = _service(tmp_path)
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
        {},
        reconciliations=Source("reconciliation"),
        updates=Source("update"),
        packages=Source("package"),
    )

    assert [worker.run_once() for _ in range(6)] == [True] * 6
    assert events == [
        "reconciliation",
        "update",
        "package",
        "reconciliation",
        "update",
        "package",
    ]
