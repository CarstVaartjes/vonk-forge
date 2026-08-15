from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select, text
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
    writer_role = threading.local()
    backend_pids: dict[str, int] = {}

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
            backend_pids["first"] = session.scalar(text("SELECT pg_backend_pid()"))
            event = repository.append_in_session(session, _draft("job-first"))
            first_allocated.set()
            assert allow_first_commit.wait(timeout=10)
        return event.id

    def second_writer() -> int:
        assert first_allocated.wait(timeout=10)
        writer_role.value = "second"
        with sessions.begin() as session:
            backend_pids["second"] = session.scalar(text("SELECT pg_backend_pid()"))
            event = repository.append_in_session(session, _draft("job-second"))
        return event.id

    def wait_for_database_block() -> tuple[list[int], str | None]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with postgres_engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT pg_blocking_pids(pid), wait_event_type
                        FROM pg_stat_activity
                        WHERE pid = :pid
                        """
                    ),
                    {"pid": backend_pids["second"]},
                ).one()
            blockers = list(row[0])
            if blockers:
                return blockers, row[1]
            time.sleep(0.05)
        pytest.fail("second allocator never became database-lock blocked")

    event.listen(postgres_engine, "before_cursor_execute", observe_lock_select)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_writer)
            assert first_allocated.wait(timeout=10)
            second = pool.submit(second_writer)
            assert second_lock_select_started.wait(timeout=10)
            try:
                blocking_pids, wait_event_type = wait_for_database_block()
                assert backend_pids["first"] in blocking_pids
                assert wait_event_type == "Lock"
            finally:
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
