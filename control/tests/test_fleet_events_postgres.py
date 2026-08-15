from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_events import FleetEventDraft, FleetEventRepository
from vonk_control.models import Base, FleetEventCursor, FleetStreamEvent

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL Fleet event ordering tests")
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-p",
                "127.0.0.1::5432",
                "postgres:16",
            ],
            text=True,
            timeout=30,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
            timeout=10,
        ).strip()
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
        )
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(
            ["docker", "stop", container],
            check=False,
            capture_output=True,
            timeout=30,
        )


def _draft(identifier: str) -> FleetEventDraft:
    return FleetEventDraft(
        event_type="operation-state",
        node_id=None,
        entity_kind="job",
        entity_id=identifier,
        payload={
            "schema_version": 1,
            "entity_kind": "job",
            "entity_id": identifier,
            "kind": "deploy",
            "state": "queued",
            "target_count": 1,
        },
    )


def test_postgres_cursor_lock_makes_ids_follow_commit_order(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    repository = FleetEventRepository(sessions, clock=lambda: NOW)

    first_allocated = threading.Event()
    allow_first_commit = threading.Event()
    second_lock_select_started = threading.Event()
    second_allocated = threading.Event()
    writer_role = threading.local()

    def observe_lock_select(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            getattr(writer_role, "value", None) == "second"
            and "SELECT fleet_event_cursor.last_id" in statement
        ):
            second_lock_select_started.set()

    def first_writer() -> int:
        with sessions.begin() as session:
            event = repository.append_in_session(session, _draft("job-first"))
            first_allocated.set()
            assert allow_first_commit.wait(timeout=10)
        return event.id

    def second_writer() -> int:
        assert first_allocated.wait(timeout=10)
        writer_role.value = "second"
        with sessions.begin() as session:
            event = repository.append_in_session(session, _draft("job-second"))
            second_allocated.set()
        return event.id

    event.listen(postgres_engine, "before_cursor_execute", observe_lock_select)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_writer)
            assert first_allocated.wait(timeout=10)
            second = pool.submit(second_writer)
            assert second_lock_select_started.wait(timeout=10)
            assert not second_allocated.wait(timeout=0.25)
            allow_first_commit.set()
            assert (first.result(timeout=10), second.result(timeout=10)) == (1, 2)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe_lock_select)

    with sessions() as session:
        rows = session.scalars(
            select(FleetStreamEvent).order_by(FleetStreamEvent.id)
        ).all()
        assert [(row.id, row.entity_id) for row in rows] == [
            (1, "job-first"),
            (2, "job-second"),
        ]
        assert session.get(FleetEventCursor, 1).last_id == 2
