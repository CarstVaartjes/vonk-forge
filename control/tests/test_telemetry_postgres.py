from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control.models import (
    AgentNode,
    Base,
    NodeTelemetryRollupBucket,
    NodeTelemetryRollupDirty,
    NodeTelemetrySample,
)
from vonk_control.telemetry import (
    TelemetryDetailsInput,
    TelemetryRepository,
    TelemetrySampleInput,
)
from vonk_control.telemetry_maintenance import TelemetryMaintenance

NODE_A = "spk_" + "a" * 32
BOOT_A = uuid.UUID("00000000-0000-4000-8000-000000000001")
BOOT_B = uuid.UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL telemetry concurrency tests")
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
        ).strip()
    except subprocess.CalledProcessError as error:
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
            ["docker", "stop", container], check=False, capture_output=True
        )


def _repository(engine: Engine) -> tuple[TelemetryRepository, sessionmaker]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
    return TelemetryRepository(sessions, clock=lambda: NOW), sessions


def _sample(
    *, boot_id: uuid.UUID, sequence: int, observed_at: datetime
) -> TelemetrySampleInput:
    return TelemetrySampleInput(
        boot_id=boot_id,
        sequence=sequence,
        observed_at=observed_at,
        cpu_utilization_percent=None,
        load_average_1m=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        temperature_c=None,
        power_watts=None,
        network_receive_bytes_per_second=None,
        network_transmit_bytes_per_second=None,
        gap_samples=0,
        details=TelemetryDetailsInput(),
    )


def _delay_first_insert(engine: Engine):
    inserted = threading.Event()
    role = threading.local()

    def delay(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if getattr(role, "value", None) == "first" and statement.startswith(
            "INSERT INTO node_telemetry_samples"
        ):
            inserted.set()
            time.sleep(0.5)

    event.listen(engine, "after_cursor_execute", delay)
    return inserted, role, delay


def test_postgres_concurrent_replay_is_idempotent(postgres_engine: Engine) -> None:
    repository, sessions = _repository(postgres_engine)
    value = _sample(
        boot_id=BOOT_A,
        sequence=1,
        observed_at=NOW - timedelta(seconds=1),
    )
    inserted, role, delay = _delay_first_insert(postgres_engine)

    def record(name: str):
        role.value = name
        return repository.record_batch(NODE_A, (value,))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(record, "first")
            assert inserted.wait(timeout=5)
            second = pool.submit(record, "second")
            results = (first.result(timeout=5), second.result(timeout=5))
    finally:
        event.remove(postgres_engine, "after_cursor_execute", delay)

    assert results[0][0].id == results[1][0].id
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(NodeTelemetrySample)) == 1


def test_postgres_out_of_order_commits_cannot_regress_latest(
    postgres_engine: Engine,
) -> None:
    repository, _ = _repository(postgres_engine)
    older = _sample(
        boot_id=BOOT_A,
        sequence=1,
        observed_at=NOW - timedelta(seconds=2),
    )
    newer = _sample(
        boot_id=BOOT_B,
        sequence=1,
        observed_at=NOW - timedelta(seconds=1),
    )
    inserted, role, delay = _delay_first_insert(postgres_engine)

    def record(name: str, value: TelemetrySampleInput):
        role.value = name
        return repository.record_batch(NODE_A, (value,))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(record, "first", older)
            assert inserted.wait(timeout=5)
            second = pool.submit(record, "second", newer)
            first.result(timeout=5)
            second.result(timeout=5)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", delay)

    latest = repository.latest((NODE_A,))[NODE_A]
    assert latest.boot_id == BOOT_B
    assert latest.observed_at == newer.observed_at


def test_postgres_late_dirty_insert_survives_claim_transaction(
    postgres_engine: Engine,
) -> None:
    repository, sessions = _repository(postgres_engine)
    repository.record_batch(
        NODE_A,
        (
            _sample(
                boot_id=BOOT_A,
                sequence=1,
                observed_at=NOW - timedelta(seconds=50),
            ),
        ),
    )
    maintenance = TelemetryMaintenance(sessions, clock=lambda: NOW)
    claim_deleted = threading.Event()
    allow_maintenance = threading.Event()
    late_dirty_started = threading.Event()
    role = threading.local()
    backend_pids: dict[str, int] = {}

    def before_statement(
        connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        current_role = getattr(role, "value", None)
        if current_role in {"maintenance", "late"}:
            backend_pids.setdefault(
                current_role,
                connection.connection.driver_connection.info.backend_pid,
            )
        if current_role == "late" and statement.lstrip().startswith(
            "INSERT INTO node_telemetry_rollup_dirty"
        ):
            late_dirty_started.set()

    def after_statement(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            getattr(role, "value", None) == "maintenance"
            and statement.lstrip().startswith(
                "DELETE FROM node_telemetry_rollup_dirty"
            )
        ):
            claim_deleted.set()
            assert allow_maintenance.wait(timeout=10)

    def run_maintenance() -> None:
        role.value = "maintenance"
        maintenance.run_once(dirty_limit=1)

    def record_late() -> None:
        role.value = "late"
        repository.record_batch(
            NODE_A,
            (
                _sample(
                    boot_id=BOOT_A,
                    sequence=2,
                    observed_at=NOW - timedelta(seconds=30),
                ),
            ),
        )

    event.listen(postgres_engine, "before_cursor_execute", before_statement)
    event.listen(postgres_engine, "after_cursor_execute", after_statement)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_maintenance)
            assert claim_deleted.wait(timeout=10)
            second = pool.submit(record_late)
            assert late_dirty_started.wait(timeout=10)

            deadline = time.monotonic() + 10
            while True:
                with postgres_engine.connect() as connection:
                    blockers, wait_event_type = connection.execute(
                        text(
                            "SELECT pg_blocking_pids(pid), wait_event_type "
                            "FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": backend_pids["late"]},
                    ).one()
                if blockers:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("late dirty insert never became lock blocked")
            assert backend_pids["maintenance"] in blockers
            assert wait_event_type == "Lock"

            allow_maintenance.set()
            first.result(timeout=10)
            second.result(timeout=10)
    finally:
        allow_maintenance.set()
        event.remove(postgres_engine, "before_cursor_execute", before_statement)
        event.remove(postgres_engine, "after_cursor_execute", after_statement)

    start = NOW.replace(second=0, microsecond=0)
    with sessions() as session:
        assert session.get(
            NodeTelemetryRollupDirty,
            (60, NODE_A, start),
        ) is not None

    maintenance.run_once(dirty_limit=1)
    with sessions() as session:
        bucket = session.get(NodeTelemetryRollupBucket, (60, NODE_A, start))
        assert bucket.source_sample_count == 2
