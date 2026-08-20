from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.worker_healthcheck import verify_worker_ready

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
NONCE = "a" * 64


def _sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-health.sqlite'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _heartbeat(*, completed_at: datetime = NOW) -> ControlProcessHeartbeat:
    return ControlProcessHeartbeat(
        process_kind="worker",
        generation_id="gen-" + "b" * 24,
        release_digest="sha256:" + "c" * 64,
        build_digest="sha256:" + "d" * 64,
        start_nonce=NONCE,
        loop_sequence=1,
        completed_at=completed_at,
    )


def test_worker_readiness_requires_a_fresh_completed_loop_with_exact_identity(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    with sessions.begin() as session:
        session.add(_heartbeat())

    verify_worker_ready(
        sessions,
        start_nonce=NONCE,
        generation_id="gen-" + "b" * 24,
        release_digest="sha256:" + "c" * 64,
        build_digest="sha256:" + "d" * 64,
        now=NOW + timedelta(seconds=5),
    )


@pytest.mark.parametrize(
    ("heartbeat", "overrides"),
    (
        (None, {}),
        (_heartbeat(completed_at=NOW - timedelta(seconds=31)), {}),
        (_heartbeat(), {"generation_id": "gen-" + "e" * 24}),
    ),
)
def test_worker_readiness_fails_closed_without_current_scheduler_evidence(
    tmp_path,
    heartbeat: ControlProcessHeartbeat | None,
    overrides: dict[str, str],
) -> None:
    sessions = _sessions(tmp_path)
    if heartbeat is not None:
        with sessions.begin() as session:
            session.add(heartbeat)
    arguments = {
        "start_nonce": NONCE,
        "generation_id": "gen-" + "b" * 24,
        "release_digest": "sha256:" + "c" * 64,
        "build_digest": "sha256:" + "d" * 64,
        "now": NOW,
    }
    arguments.update(overrides)

    with pytest.raises(RuntimeError, match="worker readiness"):
        verify_worker_ready(sessions, **arguments)
