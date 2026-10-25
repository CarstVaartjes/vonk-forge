import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control import job_states, telemetry_maintenance
from vonk_control.coordinator_failures import CoordinatorFailures
from vonk_control.jobs import JobService
from vonk_control.models import Base
from vonk_control.worker import HandlerRequest, Worker, WorkerWatchdog


def _service(
    tmp_path,
    *,
    clock: Callable[[], datetime] = lambda: datetime(2026, 8, 3, tzinfo=UTC),
):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    return JobService(
        sessionmaker(engine, expire_on_commit=False),
        clock=clock,
    )


def _failures(
    tmp_path,
    *,
    clock: Callable[[], datetime] = lambda: datetime(2026, 8, 3, tzinfo=UTC),
    source_content_sha256: str = "a" * 64,
) -> CoordinatorFailures:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    return CoordinatorFailures(
        sessionmaker(engine, expire_on_commit=False), clock, source_content_sha256
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
        coordinator_failures=_failures(tmp_path),
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

    worker = Worker(
        jobs, "worker-1", {"probe": probe}, coordinator_failures=_failures(tmp_path)
    )
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

    Worker(
        jobs, "worker-a", {"probe": handle}, coordinator_failures=_failures(tmp_path)
    ).run_once()

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
    assert (
        Worker(
            jobs, "worker-1", {}, coordinator_failures=_failures(tmp_path)
        ).run_once()
        is False
    )
    generic = jobs.enqueue("probe", "admin", "abc", [], {})
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda _: {"done": True}},
        coordinator_failures=_failures(tmp_path),
    )
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
        coordinator_failures=_failures(tmp_path),
    )
    assert worker.run_once()
    assert jobs.get(damaged.id).state in job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    worker = Worker(
        jobs,
        "worker-1",
        {"healthy": lambda _: {"done": True}},
        coordinator_failures=_failures(tmp_path),
    )
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
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
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
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
        coordinator_failures=_failures(tmp_path),
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


def test_failing_source_does_not_starve_a_healthy_durable_job(
    tmp_path, caplog, monkeypatch
) -> None:
    """A source that keeps failing must not deny unrelated jobs their turn."""

    now = [0.0]
    monkeypatch.setattr("vonk_control.worker.time.monotonic", lambda: now[0])
    epoch = datetime(2026, 8, 3, tzinfo=UTC)
    jobs = _service(tmp_path, clock=lambda: epoch + timedelta(seconds=now[0]))
    for index in range(3):
        jobs.enqueue("probe", "admin", "a" * 64, ["node"], {"index": index})

    class RecoveringSource:
        def __init__(self) -> None:
            self.calls = 0
            self.available = False

        def tick(self) -> bool:
            self.calls += 1
            if not self.available:
                raise ConnectionError("dependency unavailable")
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
        coordinator_failures=_failures(
            tmp_path, clock=lambda: epoch + timedelta(seconds=now[0])
        ),
    )

    with caplog.at_level(logging.INFO, logger="vonk-control-worker"):
        # The failing source is first in rotation every pass because the
        # durable job source advances the cursor back to it.  The job must
        # still complete on each pass.
        assert [worker.run_once() for _ in range(3)] == [True, True, True]
        assert sorted(handled) == [0, 1, 2]
        assert recipes.calls == 1
        assert "worker.source_failed" in caplog.text
        # Once the dependency recovers, the previously failing source resumes.
        recipes.available = True
        assert worker.run_once() is False
        assert recipes.calls == 1
        now[0] += 5
        assert worker.run_once() is True
        assert recipes.calls == 2
        assert sorted(handled) == [0, 1, 2]
        jobs.enqueue("probe", "admin", "a" * 64, ["node"], {"index": 3})
        assert worker.run_once() is True
        assert sorted(handled) == [0, 1, 2, 3]


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
        coordinator_failures=_failures(tmp_path),
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
        coordinator_failures=_failures(tmp_path),
    )
    assert worker.run_once()
    assert jobs.get(first.id).result == {"done": True}
    broken = False
    now[0] += 5
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    assert worker.run_once()
    assert jobs.get(fresh.id).result == {"done": True}
    # A repaired closure in the same revision is no new execution evidence.
    assert calls == [owner]
    Worker(
        jobs,
        "replacement",
        {"healthy": lambda _: {}},
        recipes=Recipes() if owner == "recipe" else None,
        housekeeping=task if owner == "housekeeping" else None,
        background_services=(task,) if owner == "background" else (),
        coordinator_failures=_failures(tmp_path, source_content_sha256="b" * 64),
    ).run_once()
    assert calls == [owner, owner]


def test_unavailable_model_cache_surface_is_local_and_fresh_work_observes_repair(
    tmp_path, monkeypatch
):
    """Catches missing adapter bookkeeping preventing unrelated/new dispatch."""

    class Cache:
        pass

    now = [datetime(2026, 8, 3, tzinfo=UTC)]
    calls = []
    jobs = _service(tmp_path)
    first = jobs.enqueue("healthy", "admin", "abc", [], {})
    worker = Worker(
        jobs,
        "worker",
        {"healthy": lambda _: {"done": True}},
        model_cache=Cache(),
        coordinator_failures=_failures(tmp_path, clock=lambda: now[0]),
    )
    assert worker.run_once()
    assert jobs.get(first.id).result == {"done": True}

    def tick(_self):
        calls.append("cache")
        return False

    monkeypatch.setattr(Cache, "tick", tick, raising=False)
    now[0] += timedelta(seconds=5)
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
            released.wait(30)
        return now[0]

    watchdog = WorkerWatchdog(timeout_seconds=30, clock=clock)
    sampling = threading.Thread(target=watchdog.beat)
    sampling.start()
    try:
        assert entered.wait(30)
        now[0] = 31.0
        assert watchdog.stalled()
    finally:
        released.set()
        sampling.join(30)
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
        jobs,
        "old",
        {},
        background_closers=(broken, lambda: calls.append("closed")),
        coordinator_failures=_failures(tmp_path),
    ).close()
    assert calls == ["broken", "broken", "broken", "closed"]
    fresh = jobs.enqueue("healthy", "admin", "abc", [], {})
    assert Worker(
        jobs,
        "new",
        {"healthy": lambda _: {"done": True}},
        coordinator_failures=_failures(tmp_path),
    ).run_once()
    assert jobs.get(fresh.id).result == {"done": True}


@pytest.mark.parametrize(
    "fault",
    [AssertionError("broken coordinator"), ConnectionError("dependency offline")],
)
def test_coordinator_fault_has_durable_api_custody_and_restart_resume(
    postgres_engine, fault
):
    """Catches JobService-coupled custody, volatile waits and unchanged retries."""
    from vonk_agent_protocol import Disposition, WaitOwnerKind, WaitResumeCondition
    from vonk_control.coordinator_contract import CoordinatorFailureEvidence
    from vonk_control.due_waits import DueWaitBackstop, WaitSource
    from vonk_control.models import CoordinatorFailure
    from vonk_control.persisted_waits import read_wait
    from vonk_control.platform_observation import PlatformObserver

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = [datetime(2026, 10, 20, tzinfo=UTC)]

    class Jobs(JobService):
        def __init__(self) -> None:
            # Failure custody belongs to the injected store, independently of jobs.
            pass

        def claim(self, *_args, **_kwargs):
            return None

    jobs = Jobs()
    calls = []
    broken = True

    def source():
        calls.append(now[0])
        if broken:
            raise fault
        return False

    worker = Worker(
        jobs,
        "old",
        {},
        background_services=(source,),
        coordinator_failures=CoordinatorFailures(sessions, lambda: now[0], "a" * 64),
    )
    assert not worker.run_once()
    with sessions() as session:
        stored = session.scalar(select(CoordinatorFailure))
        assert stored is not None and isinstance(
            stored.failure, CoordinatorFailureEvidence
        )
        wait = read_wait(stored)
        assert wait is not None
        first_blocked = wait.first_blocked_at
        assert wait.resume_condition == (
            WaitResumeCondition.DEPENDENCY_CHANGED
            if isinstance(fault, AssertionError)
            else WaitResumeCondition.RETRY_DUE
        )
    # The existing authenticated platform projection consumes the stored contract.
    visible = (
        PlatformObserver(sessions, clock=lambda: now[0]).read().coordinator_failures
    )
    assert len(visible) == 1
    assert isinstance(visible[0].failure, CoordinatorFailureEvidence)
    assert visible[0].failure.detail.endswith(str(fault))
    assert visible[0].failure.disposition == (
        Disposition.FAIL if isinstance(fault, AssertionError) else Disposition.WAIT
    )
    broken = False
    restarted = Worker(
        jobs,
        "new",
        {},
        background_services=(source,),
        coordinator_failures=CoordinatorFailures(sessions, lambda: now[0], "a" * 64),
    )
    backstop = DueWaitBackstop(
        sessions,
        (
            WaitSource(
                CoordinatorFailure,
                frozenset({WaitOwnerKind.COORDINATOR}),
                lambda key, kind: restarted.redrive_coordinator(str(key[0])),
            ),
        ),
        clock=lambda: now[0],
    )
    assert backstop.tick() == 0
    now[0] += timedelta(seconds=301)
    backstop.tick()
    if isinstance(fault, AssertionError):
        assert len(calls) == 1
        with sessions() as session:
            stored = session.scalar(select(CoordinatorFailure))
            assert stored is not None
            current_wait = read_wait(stored)
            assert (
                current_wait is not None
                and current_wait.first_blocked_at == first_blocked
            )
        # Changed source is new evidence; it re-arms through the normal worker.
        replacement = Worker(
            jobs,
            "fixed",
            {},
            background_services=(source,),
            coordinator_failures=CoordinatorFailures(
                sessions, lambda: now[0], "b" * 64
            ),
        )
        replacement.run_once()
    assert len(calls) == 2
    with sessions() as session:
        assert session.scalar(select(CoordinatorFailure)) is None


def test_durable_fault_stays_with_its_source_when_optional_services_reorder(tmp_path):
    """Catches an index-named fault blocking a different coordinator after restart."""
    jobs = _service(tmp_path)
    calls = []

    def broken():
        calls.append("broken")
        raise AssertionError("same deterministic input")

    def healthy():
        calls.append("healthy")
        return False

    assert not Worker(
        jobs,
        "first",
        {},
        background_services=(broken, healthy),
        coordinator_failures=_failures(tmp_path),
    ).run_once()
    assert not Worker(
        jobs,
        "restarted",
        {},
        background_services=(healthy, broken),
        coordinator_failures=_failures(tmp_path),
    ).run_once()
    assert calls == ["broken", "healthy", "healthy"]


def test_slow_source_yields_the_pass_while_other_work_continues(
    tmp_path, caplog, monkeypatch
) -> None:
    """Catches a pass that waits for one slow step: past the watchdog timeout
    the worker restarts, dropping in-process transfers, and every other source
    and job waits behind it."""
    from vonk_agent_protocol import ControllerErrorCode
    from vonk_control import worker as module
    from vonk_control.models import CoordinatorFailure
    from vonk_control.worker import _background_source_name

    budget = 1.0  # wall-clock: a real-time wait on the step's thread
    monkeypatch.setattr(module, "_STEP_BUDGET_SECONDS", budget, raising=False)
    now = [datetime(2026, 8, 3, tzinfo=UTC)]
    jobs = _service(tmp_path)
    release = threading.Event()
    slow_runs: list[int] = []
    healthy_runs: list[int] = []

    def slow() -> bool:
        # Fault: one step stays busy far past its budget.
        slow_runs.append(1)
        release.wait(60)
        return False

    def healthy() -> bool:
        healthy_runs.append(1)
        return False

    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda _request: {"done": True}},
        background_services=(slow, healthy),
        coordinator_failures=_failures(tmp_path, clock=lambda: now[0]),
    )
    slow_source = _background_source_name(slow)
    sessions = sessionmaker(create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}"))
    passes: list[bool] = []
    job = jobs.enqueue("probe", "admin", "a" * 64, [], {})

    def two_passes() -> None:
        passes.append(worker.run_once())
        passes.append(worker.run_once())

    try:
        with caplog.at_level(logging.INFO, logger="vonk-control-worker"):
            loop = threading.Thread(target=two_passes, daemon=True)
            loop.start()
            loop.join(30)
            # The passes returned while the slow step is still running.
            assert not loop.is_alive()
            assert not release.is_set()
        assert passes == [True, False]
        assert jobs.get(job.id).result == {"done": True}
        assert len(healthy_runs) == 2
        assert slow_runs == [1]
        assert "worker.step_over_budget" in caplog.text
        assert slow_source in caplog.text
        with sessions() as session:
            row = session.get(CoordinatorFailure, slow_source)
            assert row is not None
            assert row.failure.reason == ControllerErrorCode.TIMEOUT
    finally:
        release.set()

    # Once the slow step finishes, its outcome clears the wait and it runs again.
    worker._steps[slow_source].join(60)
    now[0] += timedelta(seconds=5)
    worker.run_once()
    with sessions() as session:
        assert session.get(CoordinatorFailure, slow_source) is None
    worker.run_once()
    assert slow_runs == [1, 1]


def test_hung_step_restarts_the_worker_and_a_fresh_request_is_served(
    tmp_path, caplog, monkeypatch
) -> None:
    """Catches a hung step that the per-step beat keeps alive forever: before
    step budgets, the watchdog restart was what healed a hang."""
    from vonk_control import worker as module
    from vonk_control.worker import _background_source_name, watch_worker

    budget = 0.2  # wall-clock: a real-time wait on the step's thread
    monkeypatch.setattr(module, "_STEP_BUDGET_SECONDS", budget)
    jobs = _service(tmp_path)
    release = threading.Event()

    def hung() -> bool:
        release.wait(60)
        return False

    watchdog = WorkerWatchdog(timeout_seconds=1.0)
    worker = Worker(
        jobs,
        "worker-1",
        {},
        background_services=(hung,),
        coordinator_failures=_failures(tmp_path),
        step_heartbeat=watchdog.beat,
    )
    stop = threading.Event()
    exits: list[int] = []
    exited = threading.Event()

    def exit_process(code: int) -> None:
        exits.append(code)
        exited.set()

    monitor = threading.Thread(
        target=watch_worker,
        args=(watchdog, worker, stop),
        kwargs={"exit_process": exit_process, "interval_seconds": 0.05},
        daemon=True,
    )
    try:
        with caplog.at_level(logging.INFO, logger="vonk-control-worker"):
            monitor.start()
            # The pass keeps turning (and beating) while the step hangs.
            while not exited.is_set() and not release.is_set():
                worker.run_once()
                exited.wait(0.05)
            assert exited.wait(30)
        assert exits == [70]
        assert "worker.step_hung" in caplog.text
        assert _background_source_name(hung) in caplog.text
    finally:
        stop.set()
        release.set()
        monitor.join(30)

    # The restarted process serves a fresh request.
    job = jobs.enqueue("probe", "admin", "a" * 64, [], {})
    restarted = Worker(
        jobs,
        "worker-2",
        {"probe": lambda _request: {"done": True}},
        coordinator_failures=_failures(tmp_path),
    )
    assert restarted.run_once() is True
    assert jobs.get(job.id).result == {"done": True}


def test_redrive_of_a_running_step_defers_instead_of_running_beside_it(
    tmp_path, monkeypatch
) -> None:
    """Catches a due-wait redrive running a service's work concurrently with
    that service's own over-budget step (unlocked in-process state)."""
    from vonk_control import worker as module
    from vonk_control.worker import StepOverBudget

    budget = 0.2  # wall-clock: a real-time wait on the step's thread
    monkeypatch.setattr(module, "_STEP_BUDGET_SECONDS", budget)
    release = threading.Event()
    calls: list[str] = []

    def tick() -> bool:
        calls.append("tick")
        release.wait(60)
        return False

    worker = Worker(
        _service(tmp_path), "worker-1", {}, coordinator_failures=_failures(tmp_path)
    )
    try:
        with pytest.raises(StepOverBudget):
            worker._bounded("storage", tick)
        with pytest.raises(StepOverBudget):
            worker._bounded(
                "storage", lambda: calls.append("redrive"), defer_if_running=True
            )
        assert calls == ["tick"]
    finally:
        release.set()
    worker._steps["storage"].join(60)
    assert worker._bounded("storage", lambda: "taken") is False  # the tick's outcome
    assert worker._bounded("storage", lambda: "redrive", defer_if_running=True) == (
        "redrive"
    )


def test_every_worker_step_and_redrive_runs_through_the_bounded_runner() -> None:
    """Catches a new step or due-wait advancer that bypasses the step budget and
    the one-at-a-time registry (an unbounded call stalls the pass again)."""
    import ast
    import inspect

    from vonk_control import worker as module

    tree = ast.parse(inspect.getsource(module))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"run_once", "_run_housekeeping", "redrive_coordinator"}
    }
    assert len(methods) == 3

    def outside_lambdas(node: ast.AST):
        # A lambda builds the step that _bounded later runs; it calls nothing.
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.Lambda):
                yield child
                yield from outside_lambdas(child)

    for method in methods.values():
        direct = [
            ast.unparse(call)
            for call in outside_lambdas(method)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in {"source", "task", "service"}
        ]
        assert direct == [], f"{method.name} calls a step outside _bounded: {direct}"
    assembly = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "assemble_production_worker"
    )
    advancers = next(
        node.value
        for node in ast.walk(assembly)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "advancers"
    )
    assert isinstance(advancers, ast.Dict)
    unbounded = [
        ast.unparse(key)
        for key, value in zip(advancers.keys, advancers.values, strict=True)
        if key is not None
        and ast.unparse(key) != "WaitOwnerKind.COORDINATOR"
        and not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "exclusive"
        )
    ]
    assert unbounded == []


def test_coordinator_redrive_runs_the_step_without_beating_the_watchdog(
    tmp_path,
) -> None:
    """Catches the due-wait step's thread beating the watchdog for a pass that
    may itself have stopped turning."""
    beats: list[int] = []
    ran: list[str] = []
    worker = Worker(
        _service(tmp_path),
        "worker-1",
        {},
        housekeeping=lambda: ran.append("telemetry"),
        coordinator_failures=_failures(tmp_path),
        step_heartbeat=lambda: beats.append(1),
    )

    worker.redrive_coordinator("housekeeping:telemetry")

    assert ran == ["telemetry"]
    assert beats == []
