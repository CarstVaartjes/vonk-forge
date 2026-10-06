from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.models import Base, Job, JobAttempt
from vonk_control.terminal_history_collection import TerminalHistoryCollector


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def history_sessions(request: pytest.FixtureRequest) -> Iterator[sessionmaker[Session]]:
    engine = (
        create_engine("sqlite:///:memory:")
        if request.param == "sqlite"
        else request.getfixturevalue("postgres_engine")
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    if request.param == "sqlite":
        engine.dispose()


def _job(
    now: datetime, *, state: str = "succeeded", payload: dict[str, object] | None = None
) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="history-test",
        state=state,
        actor="operator",
        authority_revision="revision",
        targets=[],
        payload_digest="a" * 64,
        payload=payload or {},
        current_attempt=0,
        created_at=now,
        updated_at=now,
    )


def test_terminal_history_prunes_old_rows_but_preserves_live_and_recent_references(
    history_sessions: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    removable = _job(old)
    referenced = _job(old)
    recent = _job(now)
    live = _job(old, state="running", payload={"operation_id": referenced.id})
    with history_sessions.begin() as session:
        session.add_all((removable, referenced, recent, live))
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    counts = collector.collect()
    assert counts["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, removable.id) is None
        assert set(session.scalars(select(Job.id))) == {
            referenced.id,
            recent.id,
            live.id,
        }


def test_terminal_parent_with_unreconciled_attempt_is_kept_until_attempt_ends(
    history_sessions: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    job = _job(old, state="failed")
    attempt_id = str(uuid.uuid4())
    with history_sessions.begin() as session:
        session.add(job)
        session.flush()
        session.add(
            JobAttempt(
                id=attempt_id,
                job_id=job.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                worker_id="worker",
                lease_deadline=old,
                state="running",
            )
        )
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    with history_sessions.begin() as session:
        attempt = session.get(JobAttempt, attempt_id)
        assert attempt is not None
        attempt.state = "failed"
    assert collector.collect()["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, job.id) is None
        assert session.get(JobAttempt, attempt_id) is None


def test_unreadable_live_authority_defers_pruning_until_repaired(
    history_sessions: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    removable = _job(old)
    unreadable = _job(now, state="running")
    # Empty documents are allowed fixture placeholders; the canonical reader
    # correctly reports this as a missing required recipe-operation document.
    unreadable.kind = "recipe.start"
    with history_sessions.begin() as session:
        session.add_all((removable, unreadable))
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now)
    assert not collector.collect()
    with history_sessions.begin() as session:
        assert session.get(Job, removable.id) is not None
        owner = session.get(Job, unreadable.id)
        assert owner is not None
        owner.kind = "history-test"
    assert collector.collect()["jobs"] == 1


def test_retained_oldest_rows_do_not_starve_later_unreferenced_history(
    history_sessions: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    retained = _job(now - timedelta(days=3))
    removable = _job(now - timedelta(days=2))
    live = _job(now, state="running", payload={"operation_id": retained.id})
    with history_sessions.begin() as session:
        session.add_all((retained, removable, live))
    collector = TerminalHistoryCollector(history_sessions, clock=lambda: now, batch=1)
    assert not collector.collect()
    assert collector.collect()["jobs"] == 1
    with history_sessions() as session:
        assert session.get(Job, retained.id) is not None
        assert session.get(Job, removable.id) is None
