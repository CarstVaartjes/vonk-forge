from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control.models import AgentNode, Base, NodeTelemetrySample
from vonk_control.telemetry import (
    TelemetryDetailsInput,
    TelemetryRepository,
    TelemetrySampleInput,
)

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
