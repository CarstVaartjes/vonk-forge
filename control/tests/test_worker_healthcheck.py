from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.worker import WorkerHeartbeatRecorder
from vonk_control.worker_healthcheck import verify_worker_ready

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
INSTANCE_A = "e" * 64
INSTANCE_B = "f" * 64


def _sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-health.sqlite'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _heartbeat(*, completed_at: datetime = NOW) -> ControlProcessHeartbeat:
    return ControlProcessHeartbeat(
        process_kind="worker",
        process_instance_id=INSTANCE_A,
        loop_sequence=1,
        completed_at=completed_at,
    )


def test_worker_readiness_requires_a_fresh_completed_loop_from_exact_process(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    with sessions.begin() as session:
        session.add(_heartbeat())

    verify_worker_ready(
        sessions,
        process_instance_id=INSTANCE_A,
        now=NOW + timedelta(seconds=5),
    )


@pytest.mark.parametrize(
    ("heartbeat", "process_instance_id"),
    (
        (None, INSTANCE_A),
        (_heartbeat(completed_at=NOW - timedelta(seconds=31)), INSTANCE_A),
        (_heartbeat(), INSTANCE_B),
    ),
)
def test_worker_readiness_fails_closed_without_current_scheduler_evidence(
    tmp_path,
    heartbeat: ControlProcessHeartbeat | None,
    process_instance_id: str,
) -> None:
    sessions = _sessions(tmp_path)
    if heartbeat is not None:
        with sessions.begin() as session:
            session.add(heartbeat)

    observed_error = None
    try:
        verify_worker_ready(
            sessions,
            process_instance_id=process_instance_id,
            now=NOW,
        )
    except Exception as error:  # noqa: BLE001 - readiness may be unknown
        observed_error = error
    assert observed_error is not None
    with sessions() as session:
        before = tuple(session.scalars(select(ControlProcessHeartbeat)))
        assert len(before) == (0 if heartbeat is None else 1)
    fresh = WorkerHeartbeatRecorder(
        sessions, process_instance_id=process_instance_id, clock=lambda: NOW
    )
    fresh.completed_loop()
    verify_worker_ready(sessions, process_instance_id=process_instance_id, now=NOW)
    verify_worker_ready(sessions, process_instance_id=process_instance_id, now=NOW)


def test_second_worker_instance_does_not_break_first_heartbeat(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    first = WorkerHeartbeatRecorder(
        sessions,
        process_instance_id=INSTANCE_A,
        clock=lambda: NOW,
    )
    first.completed_loop()
    verify_worker_ready(
        sessions,
        process_instance_id=INSTANCE_A,
        now=NOW + timedelta(seconds=1),
    )

    second = WorkerHeartbeatRecorder(
        sessions,
        process_instance_id=INSTANCE_B,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    second.completed_loop()
    with sessions() as session:
        heartbeats = tuple(session.scalars(select(ControlProcessHeartbeat)))
    assert {heartbeat.process_instance_id for heartbeat in heartbeats} == {
        INSTANCE_A,
        INSTANCE_B,
    }
    verify_worker_ready(
        sessions,
        process_instance_id=INSTANCE_B,
        now=NOW + timedelta(seconds=3),
    )
    first.completed_loop()
    verify_worker_ready(
        sessions,
        process_instance_id=INSTANCE_A,
        now=NOW + timedelta(seconds=3),
    )


def test_missing_heartbeat_projection_repairs_without_blocking_a_fresh_worker_loop(
    tmp_path,
):
    """Catches an absent readiness row permanently poisoning every later tick."""
    sessions = _sessions(tmp_path)
    recorder = WorkerHeartbeatRecorder(
        sessions, process_instance_id=INSTANCE_A, clock=lambda: NOW
    )
    with sessions.begin() as session:
        session.execute(delete(ControlProcessHeartbeat))
    recorder.completed_loop()
    recorder.completed_loop()
    verify_worker_ready(sessions, process_instance_id=INSTANCE_A, now=NOW)
    with sessions() as session:
        row = session.scalar(select(ControlProcessHeartbeat))
        assert row is not None and row.loop_sequence == 1
    recorder.completed_loop()
    verify_worker_ready(sessions, process_instance_id=INSTANCE_A, now=NOW)


def test_worker_readiness_accepts_controller_heartbeat_ahead_of_clock(tmp_path) -> None:
    """Catches readiness failure after a heartbeat wins the clock/read race."""
    sessions = _sessions(tmp_path)
    with sessions.begin() as session:
        session.add(_heartbeat(completed_at=NOW + timedelta(seconds=2)))
    verify_worker_ready(sessions, process_instance_id=INSTANCE_A, now=NOW)


def test_slow_step_is_published_by_the_watchdog_and_cleared_by_the_next_loop(
    tmp_path,
) -> None:
    """A step running past a third of the readiness window is named once."""
    import threading

    from vonk_control.worker import WorkerWatchdog, watch_worker

    engine = create_engine(f"sqlite:///{tmp_path / 'step.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = [0.0]
    wall = datetime(2026, 11, 1, tzinfo=UTC)
    recorder = WorkerHeartbeatRecorder(
        sessions, process_instance_id="a" * 64, clock=lambda: wall
    )
    recorder.completed_loop()
    watchdog = WorkerWatchdog(
        timeout_seconds=180, clock=lambda: now[0], wall_clock=lambda: wall
    )
    watchdog.beat("slow-step")
    stop = threading.Event()
    waits = iter([False, True])
    stop.wait = lambda _interval: next(waits)  # type: ignore[method-assign]

    def read() -> tuple[str | None, datetime | None]:
        with sessions() as session:
            row = session.scalar(select(ControlProcessHeartbeat))
            assert row is not None
            return row.running_step, row.step_started_at

    now[0] = 9
    watch_worker(
        watchdog, stop, record_step=recorder.record_running_step, exit_process=print
    )
    assert read() == (None, None)

    stop.wait = lambda _interval: next(waits2)  # type: ignore[method-assign]
    waits2 = iter([False, True])
    now[0] = 11
    watch_worker(
        watchdog, stop, record_step=recorder.record_running_step, exit_process=print
    )
    running, started = read()
    assert running == "slow-step"
    assert started is not None and started.replace(tzinfo=UTC) == wall

    recorder.completed_loop()
    assert read() == (None, None)
    engine.dispose()
