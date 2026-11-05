from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.fleet_event_contract import JobPayload
from vonk_control.fleet_events import (
    FleetEventDraft,
    FleetEventRecorder,
    FleetEventRepository,
)
from vonk_control.models import Base, FleetEventCursor, FleetStreamEvent, Job
from vonk_control.stored_json import write_guard_mode

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
WRITERS = 8
EVENTS_PER_WRITER = 25


def _draft(identifier: str) -> FleetEventDraft:
    return FleetEventDraft(
        event_type="operation-state",
        node_id=None,
        entity_kind="job",
        entity_id=identifier,
        payload=JobPayload(
            entity_kind="job",
            entity_id=identifier,
            kind="deploy",
            state="queued",
            target_count=1,
        ),
    )


def _job(identifier: str) -> Job:
    return Job(
        id=identifier,
        request_id=f"request-{identifier}",
        kind="deploy",
        state="queued",
        actor="operator@example.invalid",
        authority_revision="a" * 64,
        targets=["spk_" + "a" * 32],
        payload_digest="b" * 64,
        payload={},
        result=None,
        status_reason=None,
        current_attempt=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _fresh_sessions(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _stored(sessions: sessionmaker[Session]) -> list[tuple[int, str]]:
    with sessions() as session:
        return [
            (row.id, row.entity_id)
            for row in session.scalars(
                select(FleetStreamEvent).order_by(FleetStreamEvent.id)
            )
        ]


def test_open_transaction_with_events_does_not_block_another_writer(
    postgres_engine: Engine,
) -> None:
    sessions = _fresh_sessions(postgres_engine)
    repository = FleetEventRepository(sessions, clock=lambda: NOW)
    first_flushed = threading.Event()
    second_committed = threading.Event()

    def first_writer() -> None:
        with sessions.begin() as session:
            repository.append_in_session(session, _draft("job-first"))
            session.flush()
            first_flushed.set()
            # Still open: any cursor lock held here would stop the second writer.
            assert second_committed.wait(timeout=10)

    def second_writer() -> None:
        assert first_flushed.wait(timeout=10)
        with sessions.begin() as session:
            # A wait for the open first transaction fails fast instead of hanging.
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            repository.append_in_session(session, _draft("job-second"))
        second_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_writer)
        second = pool.submit(second_writer)
        second.result(timeout=10)
        first.result(timeout=10)

    # Ids follow commit order, so a capture boundary never hides a later id.
    assert _stored(sessions) == [(1, "job-second"), (2, "job-first")]
    with sessions() as session:
        cursor = session.get(FleetEventCursor, 1)
        assert cursor is not None
        assert cursor.last_id == 2


def test_concurrent_appends_reach_a_live_reader_once_in_order_without_gaps(
    postgres_engine: Engine,
) -> None:
    sessions = _fresh_sessions(postgres_engine)
    repository = FleetEventRepository(sessions, clock=lambda: NOW)
    start = threading.Barrier(WRITERS + 1)
    writers_done = threading.Event()

    def writer(index: int) -> None:
        pause = random.Random(index)
        start.wait(timeout=10)
        for sequence in range(EVENTS_PER_WRITER):
            with sessions.begin() as session:
                # Writers queue only for one another's commit, never for open work.
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                repository.append_in_session(
                    session, _draft(f"writer-{index}-{sequence}")
                )
                session.flush()
                time.sleep(pause.uniform(0, 0.01))

    def reader() -> list[tuple[int, str]]:
        # Polls like FleetStream: any hole raises, so passing proves none showed.
        cursor = 0
        seen: list[tuple[int, str]] = []
        start.wait(timeout=10)
        while True:
            done = writers_done.is_set()
            replay = repository.replay_after(cursor, NOW, limit=128)
            for value in replay.events:
                seen.append((value.id, value.entity_id))
                cursor = value.id
            if done and not replay.events:
                return seen
            if not replay.events:
                time.sleep(0.001)

    with ThreadPoolExecutor(max_workers=WRITERS + 1) as pool:
        observed = pool.submit(reader)
        written = [pool.submit(writer, index) for index in range(WRITERS)]
        for result in written:
            result.result(timeout=60)
        writers_done.set()
        seen = observed.result(timeout=60)

    expected = {
        f"writer-{index}-{sequence}"
        for index in range(WRITERS)
        for sequence in range(EVENTS_PER_WRITER)
    }
    total = WRITERS * EVENTS_PER_WRITER
    assert [event_id for event_id, _ in seen] == list(range(1, total + 1))
    assert sorted(entity for _, entity in seen) == sorted(expected)
    assert _stored(sessions) == seen
    assert repository.high_watermark() == total


def test_savepoint_rollback_discards_only_the_events_it_staged(
    postgres_engine: Engine,
) -> None:
    sessions = _fresh_sessions(postgres_engine)
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    repository = FleetEventRepository(sessions, clock=lambda: NOW)
    with write_guard_mode(strict=False), sessions.begin() as session:
        session.add(_job("job-outer"))
        session.flush()
        with pytest.raises(RuntimeError), session.begin_nested():
            session.add(_job("job-rolled-back"))
            session.flush()
            raise RuntimeError("savepoint refused")
        with pytest.raises(RuntimeError), session.begin_nested():
            with session.begin_nested():
                repository.append_in_session(session, _draft("inner-released"))
            raise RuntimeError("outer savepoint refused")
        with session.begin_nested():
            session.add(_job("job-released"))

    assert _stored(sessions) == [(1, "job-outer"), (2, "job-released")]
    with sessions() as session:
        assert session.get(Job, "job-rolled-back") is None
        cursor = session.get(FleetEventCursor, 1)
        assert cursor is not None
        assert cursor.last_id == 2
