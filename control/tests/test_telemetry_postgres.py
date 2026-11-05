from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, func, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.telemetry import HostResourceSample
from vonk_control.inventory_items_contract import InventorySnapshotKind
from vonk_control.models import (
    AgentNode,
    Base,
    NodeInventoryResourceSnapshot,
    NodeTelemetrySample,
)
from vonk_control.telemetry import (
    TelemetryRepository,
    TelemetrySample,
)
from vonk_control.telemetry_maintenance import TelemetryMaintenance, _polite

NODE_A = "spk_" + "a" * 32
BOOT_A = uuid.UUID("00000000-0000-4000-8000-000000000001")
BOOT_B = uuid.UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _repository(engine: Engine) -> tuple[TelemetryRepository, sessionmaker]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active"))
    return TelemetryRepository(sessions, clock=lambda: NOW), sessions


def _sample(
    *, boot_id: uuid.UUID, sequence: int, observed_at: datetime
) -> TelemetrySample:
    return TelemetrySample.model_validate_json(
        canonical_message(
            {
                "boot_id": str(boot_id),
                "observed_at": observed_at,
                "memory_total_bytes": None,
                "memory_available_bytes": None,
                "disk_total_bytes": None,
                "disk_free_bytes": None,
                "gpu_utilization_percent": None,
                "gpu_memory_total_bytes": None,
                "gpu_memory_free_bytes": None,
            }
        )
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
            assert inserted.wait(timeout=30)
            second = pool.submit(record, "second")
            results = (first.result(timeout=30), second.result(timeout=30))
    finally:
        event.remove(postgres_engine, "after_cursor_execute", delay)

    assert results[0][0].id == results[1][0].id
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(NodeTelemetrySample)) == 1
        )


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

    def record(name: str, value: TelemetrySample):
        role.value = name
        return repository.record_batch(NODE_A, (value,))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(record, "first", older)
            assert inserted.wait(timeout=30)
            second = pool.submit(record, "second", newer)
            first.result(timeout=30)
            second.result(timeout=30)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", delay)

    latest = repository.latest((NODE_A,))[NODE_A]
    assert latest.boot_id == str(BOOT_B)
    assert latest.observed_at == newer.observed_at


def test_gpu_clock_observation_roundtrips_postgres_and_old_rows_remain_null(
    postgres_engine: Engine,
) -> None:
    """Catches untyped JSON, missing observation writes and upgrade row rewrites."""

    from vonk_agent_protocol.gpu_clock import (
        GpuClockApplyState,
        GpuClockMetric,
        GpuClockObservation,
        GpuClockUnavailable,
        GpuClockUnavailableReason,
        default_gpu_clock,
    )

    repository, sessions = _repository(postgres_engine)
    first = _sample(boot_id=BOOT_A, sequence=1, observed_at=NOW - timedelta(seconds=2))
    repository.record_batch(NODE_A, (first,))
    clock = GpuClockObservation(
        graphics_mhz=1800,
        sm_mhz=1790,
        locked_range=default_gpu_clock(),
        throttle_reasons=0,
        max_graphics_mhz=2400,
        max_sm_mhz=2300,
        apply_state=GpuClockApplyState.APPLIED,
        unavailable=[
            GpuClockUnavailable(
                metric=GpuClockMetric.THROTTLE,
                reason=GpuClockUnavailableReason.NOT_SUPPORTED,
                detail="driver does not support throttle reasons",
            )
        ],
    )
    second = type(first).model_validate(
        {
            **first.model_dump(),
            "observed_at": NOW - timedelta(seconds=1),
            "gpu_clock": clock,
        }
    )
    repository.record_batch(NODE_A, (second,))
    with sessions() as session:
        rows = list(
            session.scalars(
                select(NodeTelemetrySample).order_by(NodeTelemetrySample.observed_at)
            )
        )
        assert rows[0].gpu_clock is None
        from sqlalchemy import text

        assert (
            session.scalar(
                text(
                    "SELECT gpu_clock IS NULL FROM node_telemetry_samples WHERE id = :id"
                ),
                {"id": rows[0].id},
            )
            is True
        )
        assert rows[1].gpu_clock == clock
    assert repository.latest((NODE_A,))[NODE_A].gpu_clock == clock


def test_gpu_clock_nullable_column_is_added_in_place_without_rewriting_old_samples(
    postgres_engine: Engine,
) -> None:
    """The approved upgrade preserves old samples and the existing latest pointer."""
    from sqlalchemy import text
    from vonk_control.db import reconcile_schema

    repository, _sessions = _repository(postgres_engine)
    value = _sample(boot_id=BOOT_A, sequence=1, observed_at=NOW - timedelta(seconds=1))
    original = repository.record_batch(NODE_A, (value,))[0]
    with postgres_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE node_telemetry_samples DROP COLUMN gpu_clock")
        )
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
    restored = repository.latest((NODE_A,))[NODE_A]
    assert restored.id == original.id
    assert restored.gpu_clock is None
    assert restored.observed_at == original.observed_at


def test_postgres_measurement_drain_is_bounded_per_pass_and_skips_locked_rows(
    postgres_engine: Engine,
) -> None:
    """Production, 2026-11-04: 923,782 orphan measurements, no cascade key.

    The drain deletes at most the pass limit, oldest first, with the same
    number of statements whatever the backlog, and never waits for a row
    another transaction holds.
    """

    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    node_b = "spk_" + "b" * 32
    with sessions.begin() as session:
        session.execute(
            text(
                "ALTER TABLE node_inventory_snapshots "
                "DROP CONSTRAINT fk_inventory_resource_sample"
            )
        )
        session.add_all(
            (
                AgentNode(node_id=NODE_A, state="active"),
                AgentNode(node_id=node_b, state="active"),
            )
        )
    cutoff = NOW - timedelta(hours=24)
    resources = HostResourceSample(
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
    )
    seeded = 0

    def seed(node_id: str, count: int, *, newest: datetime) -> None:
        nonlocal seeded
        rows = []
        for offset in range(count):
            seeded += 1
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "node_id": node_id,
                    "kind": InventorySnapshotKind.MEASUREMENTS,
                    "observed_at": newest - timedelta(seconds=offset),
                    "received_at": newest - timedelta(seconds=offset),
                    "source_sample_id": str(uuid.uuid4()),
                    "host_resources": resources,
                    "evidence_digest": hashlib.sha256(str(seeded).encode()).hexdigest(),
                }
            )
        with sessions.begin() as session:
            session.execute(insert(NodeInventoryResourceSnapshot), rows)

    def count() -> int:
        with sessions() as session:
            return session.scalars(
                select(func.count()).select_from(NodeInventoryResourceSnapshot)
            ).one()

    def drain(limit: int) -> tuple[int, int]:
        """One maintenance pass of the stage: (rows deleted, statements)."""

        statements: list[str] = []

        def capture(_connection, _cursor, statement, *_args) -> None:
            statements.append(statement)

        before = count()
        event.listen(postgres_engine, "before_cursor_execute", capture)
        try:
            with sessions.begin() as session:
                _polite(session, "telemetry-maintenance")
                TelemetryMaintenance._prune_measurements(
                    session, cutoff=cutoff, limit=limit
                )
        finally:
            event.remove(postgres_engine, "before_cursor_execute", capture)
        return before - count(), len(statements)

    old = cutoff - timedelta(hours=1)
    recent = NOW - timedelta(minutes=10)
    seed(NODE_A, 600, newest=old)
    seed(node_b, 600, newest=old)
    seed(NODE_A, 10, newest=recent)

    deleted, small_backlog_statements = drain(500)
    assert deleted == 500

    seed(NODE_A, 3_000, newest=old - timedelta(days=1))
    seed(node_b, 3_000, newest=old - timedelta(days=1))
    deleted, large_backlog_statements = drain(500)
    assert deleted == 500
    assert large_backlog_statements == small_backlog_statements

    # A row another transaction holds is skipped, never waited for.
    with sessions.begin() as holder:
        held = holder.scalar(
            select(NodeInventoryResourceSnapshot.id)
            .where(NodeInventoryResourceSnapshot.node_id == NODE_A)
            .order_by(NodeInventoryResourceSnapshot.observed_at)
            .limit(1)
            .with_for_update()
        )
        deleted, _statements = drain(500)
        assert deleted == 500
        with sessions() as session:
            assert session.get(NodeInventoryResourceSnapshot, held) is not None

    while drain(500)[0]:
        pass
    assert count() == 10
