from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
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


def test_worker_readiness_requires_a_fresh_completed_loop_from_exact_process(tmp_path) -> None:
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

    with pytest.raises(RuntimeError, match="worker readiness"):
        verify_worker_ready(
            sessions,
            process_instance_id=process_instance_id,
            now=NOW,
        )


def test_worker_restart_immediately_revokes_prior_process_readiness(tmp_path) -> None:
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

    with pytest.raises(RuntimeError, match="worker readiness"):
        verify_worker_ready(
            sessions,
            process_instance_id=INSTANCE_B,
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(RuntimeError, match="process instance"):
        first.completed_loop()

    second.completed_loop()
    verify_worker_ready(
        sessions,
        process_instance_id=INSTANCE_B,
        now=NOW + timedelta(seconds=3),
    )
