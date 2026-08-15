from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.fleet_events import FleetEventRepository
from vonk_control.fleet_projection import FleetSnapshot
from vonk_control.fleet_stream import FleetStream
from vonk_control.models import (
    AgentNode,
    Base,
    FleetEventCursor,
    FleetStreamEvent,
    NodeTelemetryLatest,
    NodeTelemetryRollupBucket,
    NodeTelemetryRollupDirty,
    NodeTelemetryRollupMetric,
    NodeTelemetrySample,
)
from vonk_control.telemetry import (
    TelemetryDetailsInput,
    TelemetryRepository,
    TelemetrySampleInput,
)

NODE_A = "spk_" + "a" * 32
BOOT_A = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 8, 15, 12, 4, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'maintenance.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
    return factory


def _raw(
    identifier: str,
    observed_at: datetime,
    *,
    sequence: int,
    cpu: float | None = None,
    temperature: float | None = None,
    gap_samples: int = 0,
) -> NodeTelemetrySample:
    return NodeTelemetrySample(
        id=identifier,
        node_id=NODE_A,
        boot_id=BOOT_A,
        sequence=sequence,
        observed_at=observed_at,
        received_at=observed_at,
        cpu_utilization_percent=cpu,
        load_average_1m=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        temperature_c=temperature,
        power_watts=None,
        network_receive_bytes_per_second=None,
        network_transmit_bytes_per_second=None,
        gap_samples=gap_samples,
        details={},
    )


def _input(
    *, sequence: int, observed_at: datetime, cpu: float
) -> TelemetrySampleInput:
    return TelemetrySampleInput(
        boot_id=uuid.UUID(BOOT_A),
        sequence=sequence,
        observed_at=observed_at,
        cpu_utilization_percent=cpu,
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


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_dirty_claim_is_one_ordered_bounded_delete_returning(dialect) -> None:
    statement = telemetry_maintenance._claim_dirty_statement(7)

    sql = " ".join(
        str(
            statement.compile(
                dialect=dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        .lower()
        .split()
    )

    assert sql.startswith("delete from node_telemetry_rollup_dirty where")
    assert "select node_telemetry_rollup_dirty.resolution_seconds" in sql
    assert "order by node_telemetry_rollup_dirty.resolution_seconds" in sql
    assert "node_telemetry_rollup_dirty.bucket_start" in sql
    assert "node_telemetry_rollup_dirty.node_id" in sql
    assert "limit 7" in sql
    returning = sql.partition(" returning ")[2]
    assert returning
    assert "resolution_seconds" in returning
    assert "node_id" in returning
    assert "bucket_start" in returning


def test_claimed_dirty_rows_are_recomputed_in_deterministic_key_order(
    monkeypatch,
) -> None:
    minute_start = datetime(2026, 8, 15, 11, 59, tzinfo=UTC)
    quarter_start = datetime(2026, 8, 15, 11, 45, tzinfo=UTC)
    recomputed: list[tuple[int, str, datetime]] = []

    class Result:
        def all(self):
            return [
                (900, NODE_A, quarter_start),
                (60, NODE_A, minute_start),
            ]

    class Session:
        def execute(self, _statement):
            return Result()

    class Transaction:
        def __enter__(self):
            return Session()

        def __exit__(self, *_error) -> None:
            return None

    class Sessions:
        def begin(self):
            return Transaction()

    monkeypatch.setattr(
        telemetry_maintenance.TelemetryMaintenance,
        "_recompute_minute",
        staticmethod(
            lambda _session, node_id, start: recomputed.append(
                (60, node_id, start)
            )
        ),
    )
    monkeypatch.setattr(
        telemetry_maintenance.TelemetryMaintenance,
        "_recompute_quarter_hour",
        staticmethod(
            lambda _session, node_id, start: recomputed.append(
                (900, node_id, start)
            )
        ),
    )
    monkeypatch.setattr(
        telemetry_maintenance.TelemetryMaintenance,
        "_prune",
        staticmethod(lambda _session, _now, _limit, _events: None),
    )

    telemetry_maintenance.TelemetryMaintenance(
        Sessions(), clock=lambda: NOW
    ).run_once()

    assert recomputed == [
        (60, NODE_A, minute_start),
        (900, NODE_A, quarter_start),
    ]


def test_minute_recompute_is_half_open_independent_and_idempotent(
    sessions,
) -> None:
    start = datetime(2026, 8, 15, 11, 59, tzinfo=UTC)
    with sessions.begin() as session:
        session.add_all(
            (
                _raw(
                    "outside-before",
                    start - timedelta(microseconds=1),
                    sequence=1,
                    cpu=99,
                    gap_samples=50,
                ),
                _raw(
                    "inside-1",
                    start,
                    sequence=2,
                    cpu=10,
                    gap_samples=1,
                ),
                _raw(
                    "inside-2",
                    start + timedelta(seconds=20),
                    sequence=3,
                    cpu=20,
                    temperature=30,
                    gap_samples=2,
                ),
                _raw(
                    "inside-3",
                    start + timedelta(seconds=59, microseconds=999999),
                    sequence=4,
                    temperature=40,
                    gap_samples=3,
                ),
                _raw(
                    "outside-after",
                    start + timedelta(seconds=60),
                    sequence=5,
                    cpu=88,
                    gap_samples=60,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=start,
                ),
            )
        )

    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    )
    maintenance.run_once(dirty_limit=1)

    with sessions() as session:
        bucket = session.get(
            NodeTelemetryRollupBucket,
            (60, NODE_A, start),
        )
        metrics = {
            metric.metric_name: metric
            for metric in session.scalars(
                select(NodeTelemetryRollupMetric).where(
                    NodeTelemetryRollupMetric.resolution_seconds == 60,
                    NodeTelemetryRollupMetric.node_id == NODE_A,
                    NodeTelemetryRollupMetric.bucket_start == start,
                )
            )
        }
        dirty = session.scalars(select(NodeTelemetryRollupDirty)).all()

    assert bucket is not None
    assert (bucket.source_sample_count, bucket.gap_samples) == (3, 6)
    assert set(metrics) == {"cpu_utilization_percent", "temperature_c"}
    assert (
        metrics["cpu_utilization_percent"].sample_count,
        metrics["cpu_utilization_percent"].minimum,
        metrics["cpu_utilization_percent"].mean,
        metrics["cpu_utilization_percent"].maximum,
    ) == (2, 10, 15, 20)
    assert (
        metrics["temperature_c"].sample_count,
        metrics["temperature_c"].minimum,
        metrics["temperature_c"].mean,
        metrics["temperature_c"].maximum,
    ) == (2, 30, 35, 40)
    assert [
        (
            item.resolution_seconds,
            item.node_id,
            item.bucket_start.replace(tzinfo=UTC),
        )
        for item in dirty
    ] == [(900, NODE_A, datetime(2026, 8, 15, 11, 45, tzinfo=UTC))]

    with sessions.begin() as session:
        session.add(
            NodeTelemetryRollupDirty(
                resolution_seconds=60,
                node_id=NODE_A,
                bucket_start=start,
            )
        )
    maintenance.run_once(dirty_limit=1)

    with sessions() as session:
        assert session.get(
            NodeTelemetryRollupBucket,
            (60, NODE_A, start),
        ).source_sample_count == 3
        assert len(
            session.scalars(
                select(NodeTelemetryRollupMetric).where(
                    NodeTelemetryRollupMetric.resolution_seconds == 60,
                    NodeTelemetryRollupMetric.node_id == NODE_A,
                    NodeTelemetryRollupMetric.bucket_start == start,
                )
            ).all()
        ) == 2
        assert len(session.scalars(select(NodeTelemetryRollupDirty)).all()) == 1


def test_empty_minute_recompute_removes_stale_bucket_and_queues_parent(
    sessions,
) -> None:
    start = datetime(2026, 8, 15, 11, 59, tzinfo=UTC)
    with sessions.begin() as session:
        session.add_all(
            (
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=start,
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupMetric(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=start,
                    metric_name="cpu_utilization_percent",
                    sample_count=1,
                    minimum=10,
                    mean=10,
                    maximum=10,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=start,
                ),
            )
        )

    telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    ).run_once(dirty_limit=1)

    with sessions() as session:
        assert session.get(
            NodeTelemetryRollupBucket,
            (60, NODE_A, start),
        ) is None
        assert session.get(
            NodeTelemetryRollupMetric,
            (60, NODE_A, start, "cpu_utilization_percent"),
        ) is None
        assert session.get(
            NodeTelemetryRollupDirty,
            (
                900,
                NODE_A,
                datetime(2026, 8, 15, 11, 45, tzinfo=UTC),
            ),
        ) is not None


def test_run_once_captures_one_aware_clock_value_and_rejects_unbounded_limits(
    sessions,
) -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=clock
    )
    maintenance.run_once()
    assert calls == 1

    for dirty_limit, delete_limit in ((0, 1), (25_001, 1), (1, 0), (1, 25_001)):
        with pytest.raises(ValueError, match="limit"):
            maintenance.run_once(
                dirty_limit=dirty_limit,
                delete_limit=delete_limit,
            )
    assert calls == 1

    with pytest.raises(ValueError, match="timezone-aware"):
        telemetry_maintenance.TelemetryMaintenance(
            sessions,
            clock=lambda: NOW.replace(tzinfo=None),
        ).run_once()


def test_quarter_hour_recompute_weights_minute_means_by_metric_count(
    sessions,
) -> None:
    start = datetime(2026, 8, 15, 11, 45, tzinfo=UTC)
    minute_1 = start
    minute_2 = start + timedelta(minutes=1)
    minute_3 = start + timedelta(minutes=2)
    outside = start + timedelta(minutes=15)
    with sessions.begin() as session:
        session.add_all(
            (
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_1,
                    source_sample_count=1,
                    gap_samples=1,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_2,
                    source_sample_count=3,
                    gap_samples=2,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_3,
                    source_sample_count=2,
                    gap_samples=3,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=outside,
                    source_sample_count=100,
                    gap_samples=100,
                ),
                NodeTelemetryRollupMetric(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_1,
                    metric_name="cpu_utilization_percent",
                    sample_count=1,
                    minimum=10,
                    mean=10,
                    maximum=10,
                ),
                NodeTelemetryRollupMetric(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_2,
                    metric_name="cpu_utilization_percent",
                    sample_count=3,
                    minimum=20,
                    mean=30,
                    maximum=40,
                ),
                NodeTelemetryRollupMetric(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_3,
                    metric_name="temperature_c",
                    sample_count=2,
                    minimum=45,
                    mean=50,
                    maximum=55,
                ),
                NodeTelemetryRollupMetric(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=outside,
                    metric_name="cpu_utilization_percent",
                    sample_count=100,
                    minimum=99,
                    mean=99,
                    maximum=99,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=900,
                    node_id=NODE_A,
                    bucket_start=start,
                ),
            )
        )

    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    )
    maintenance.run_once(dirty_limit=1)

    with sessions() as session:
        bucket = session.get(NodeTelemetryRollupBucket, (900, NODE_A, start))
        metrics = {
            metric.metric_name: metric
            for metric in session.scalars(
                select(NodeTelemetryRollupMetric).where(
                    NodeTelemetryRollupMetric.resolution_seconds == 900,
                    NodeTelemetryRollupMetric.node_id == NODE_A,
                    NodeTelemetryRollupMetric.bucket_start == start,
                )
            )
        }
        assert session.scalars(select(NodeTelemetryRollupDirty)).all() == []

    assert bucket is not None
    assert (bucket.source_sample_count, bucket.gap_samples) == (6, 6)
    assert set(metrics) == {"cpu_utilization_percent", "temperature_c"}
    assert (
        metrics["cpu_utilization_percent"].sample_count,
        metrics["cpu_utilization_percent"].minimum,
        metrics["cpu_utilization_percent"].mean,
        metrics["cpu_utilization_percent"].maximum,
    ) == (4, 10, 25, 40)
    assert (
        metrics["temperature_c"].sample_count,
        metrics["temperature_c"].minimum,
        metrics["temperature_c"].mean,
        metrics["temperature_c"].maximum,
    ) == (2, 45, 50, 55)


def test_late_sample_reruns_minute_and_quarter_hour_rollups(sessions) -> None:
    minute_start = datetime(2026, 8, 15, 11, 59, tzinfo=UTC)
    quarter_start = datetime(2026, 8, 15, 11, 45, tzinfo=UTC)
    with sessions.begin() as session:
        session.add_all(
            (
                _raw(
                    "initial",
                    minute_start,
                    sequence=1,
                    cpu=10,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_start,
                ),
            )
        )
    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    )
    maintenance.run_once(dirty_limit=1)
    maintenance.run_once(dirty_limit=1)

    with sessions.begin() as session:
        session.add_all(
            (
                _raw(
                    "late",
                    minute_start + timedelta(seconds=30),
                    sequence=2,
                    cpu=30,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_start,
                ),
            )
        )
    maintenance.run_once(dirty_limit=1)
    maintenance.run_once(dirty_limit=1)

    with sessions() as session:
        minute = session.scalar(
            select(NodeTelemetryRollupMetric).where(
                NodeTelemetryRollupMetric.resolution_seconds == 60,
                NodeTelemetryRollupMetric.metric_name
                == "cpu_utilization_percent",
            )
        )
        quarter = session.scalar(
            select(NodeTelemetryRollupMetric).where(
                NodeTelemetryRollupMetric.resolution_seconds == 900,
                NodeTelemetryRollupMetric.bucket_start == quarter_start,
                NodeTelemetryRollupMetric.metric_name
                == "cpu_utilization_percent",
            )
        )

    assert (minute.sample_count, minute.mean) == (2, 20)
    assert (quarter.sample_count, quarter.mean) == (2, 20)


def test_retention_boundaries_are_strict_ordered_and_repeatedly_bounded(
    sessions,
) -> None:
    raw_cutoff = NOW - timedelta(hours=24)
    minute_cutoff = telemetry_maintenance.bucket_start(
        NOW - timedelta(days=30), 60
    )
    quarter_cutoff = telemetry_maintenance.bucket_start(
        NOW - timedelta(days=365), 900
    )
    with sessions.begin() as session:
        session.add_all(
            (
                _raw(
                    "raw-old-1",
                    raw_cutoff - timedelta(seconds=2),
                    sequence=1,
                ),
                _raw(
                    "raw-old-2",
                    raw_cutoff - timedelta(seconds=1),
                    sequence=2,
                ),
                _raw("raw-boundary", raw_cutoff, sequence=3),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_cutoff - timedelta(minutes=2),
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_cutoff - timedelta(minutes=1),
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=minute_cutoff,
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=900,
                    node_id=NODE_A,
                    bucket_start=quarter_cutoff - timedelta(minutes=30),
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=900,
                    node_id=NODE_A,
                    bucket_start=quarter_cutoff - timedelta(minutes=15),
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=900,
                    node_id=NODE_A,
                    bucket_start=quarter_cutoff,
                    source_sample_count=1,
                    gap_samples=0,
                ),
                FleetStreamEvent(
                    id=1,
                    event_type="operation-state",
                    node_id=None,
                    entity_kind="job",
                    entity_id="job-1",
                    payload={"schema_version": 1},
                    occurred_at=NOW - timedelta(hours=1),
                    expires_at=NOW - timedelta(seconds=1),
                ),
                FleetStreamEvent(
                    id=2,
                    event_type="operation-state",
                    node_id=None,
                    entity_kind="job",
                    entity_id="job-2",
                    payload={"schema_version": 1},
                    occurred_at=NOW - timedelta(hours=1),
                    expires_at=NOW,
                ),
                FleetStreamEvent(
                    id=3,
                    event_type="operation-state",
                    node_id=None,
                    entity_kind="job",
                    entity_id="job-3",
                    payload={"schema_version": 1},
                    occurred_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(seconds=1),
                ),
            )
        )
        session.execute(update(FleetEventCursor).values(last_id=3))

    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    )
    maintenance.run_once(dirty_limit=1, delete_limit=1)

    with sessions() as session:
        assert session.scalars(
            select(NodeTelemetrySample.id).order_by(NodeTelemetrySample.observed_at)
        ).all() == ["raw-old-2", "raw-boundary"]
        assert session.scalars(
            select(NodeTelemetryRollupBucket.bucket_start)
            .where(NodeTelemetryRollupBucket.resolution_seconds == 60)
            .order_by(NodeTelemetryRollupBucket.bucket_start)
        ).all() == [
            (minute_cutoff - timedelta(minutes=1)).replace(tzinfo=None),
            minute_cutoff.replace(tzinfo=None),
        ]
        assert session.scalars(
            select(NodeTelemetryRollupBucket.bucket_start)
            .where(NodeTelemetryRollupBucket.resolution_seconds == 900)
            .order_by(NodeTelemetryRollupBucket.bucket_start)
        ).all() == [
            (quarter_cutoff - timedelta(minutes=15)).replace(tzinfo=None),
            quarter_cutoff.replace(tzinfo=None),
        ]
        assert session.scalars(
            select(FleetStreamEvent.id).order_by(FleetStreamEvent.id)
        ).all() == [2, 3]
        assert session.get(FleetEventCursor, 1).last_id == 3

    maintenance.run_once(dirty_limit=1, delete_limit=1)

    with sessions() as session:
        assert session.scalars(select(NodeTelemetrySample.id)).all() == [
            "raw-boundary"
        ]
        assert session.scalars(
            select(NodeTelemetryRollupBucket.bucket_start).where(
                NodeTelemetryRollupBucket.resolution_seconds == 60
            )
        ).all() == [minute_cutoff.replace(tzinfo=None)]
        assert session.scalars(
            select(NodeTelemetryRollupBucket.bucket_start).where(
                NodeTelemetryRollupBucket.resolution_seconds == 900
            )
        ).all() == [quarter_cutoff.replace(tzinfo=None)]
        assert session.scalars(select(FleetStreamEvent.id)).all() == [3]
        assert session.get(FleetEventCursor, 1).last_id == 3


def test_retention_keeps_sources_with_outstanding_rollup_work(sessions) -> None:
    raw_cutoff = NOW - timedelta(hours=24)
    protected_raw_start = telemetry_maintenance.bucket_start(
        raw_cutoff - timedelta(minutes=2), 60
    )
    minute_cutoff = telemetry_maintenance.bucket_start(
        NOW - timedelta(days=30), 60
    )
    protected_minute_start = minute_cutoff - timedelta(minutes=2)
    protected_parent = telemetry_maintenance.bucket_start(
        protected_minute_start, 900
    )
    clean_minute_start = minute_cutoff - timedelta(minutes=16)
    with sessions.begin() as session:
        session.add_all(
            (
                _raw(
                    "raw-clean",
                    raw_cutoff - timedelta(minutes=3),
                    sequence=1,
                ),
                _raw(
                    "raw-dirty",
                    raw_cutoff - timedelta(minutes=2),
                    sequence=2,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=protected_minute_start,
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupBucket(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=clean_minute_start,
                    source_sample_count=1,
                    gap_samples=0,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=protected_raw_start - timedelta(hours=1),
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=protected_raw_start,
                ),
                NodeTelemetryRollupDirty(
                    resolution_seconds=900,
                    node_id=NODE_A,
                    bucket_start=protected_parent,
                ),
            )
        )

    telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    ).run_once(dirty_limit=1, delete_limit=100)

    with sessions() as session:
        assert session.scalars(select(NodeTelemetrySample.id)).all() == [
            "raw-dirty"
        ]
        assert session.get(
            NodeTelemetryRollupBucket,
            (60, NODE_A, protected_minute_start),
        ) is not None
        assert session.get(
            NodeTelemetryRollupBucket,
            (60, NODE_A, clean_minute_start),
        ) is None


def test_latest_raw_pruning_appends_authoritative_missing_sample_reset(
    sessions,
) -> None:
    sample_id = "latest-expired"
    with sessions.begin() as session:
        session.add(
            _raw(
                sample_id,
                NOW - timedelta(hours=24, microseconds=1),
                sequence=1,
                cpu=10,
            )
        )
        session.add(NodeTelemetryLatest(node_id=NODE_A, sample_id=sample_id))

    telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    ).run_once(delete_limit=1)

    with sessions() as session:
        assert session.get(NodeTelemetryLatest, NODE_A) is None
        assert session.get(NodeTelemetrySample, sample_id) is None
        event = session.get(FleetStreamEvent, 1)
        assert event is not None
        assert (
            event.event_type,
            event.node_id,
            event.entity_kind,
            event.entity_id,
            event.payload,
        ) == (
            "node-telemetry",
            NODE_A,
            "node-telemetry-latest",
            NODE_A,
            {
                "schema_version": 1,
                "node_id": NODE_A,
                "sample_id": sample_id,
            },
        )
        assert event.occurred_at.replace(tzinfo=UTC) == NOW
        assert event.expires_at.replace(tzinfo=UTC) == NOW + timedelta(hours=24)
        assert session.get(FleetEventCursor, 1).last_id == 1
    assert TelemetryRepository(sessions, clock=lambda: NOW).latest((NODE_A,)) == {}

    class Projection:
        def read_at(self, cursor: int) -> FleetSnapshot:
            return FleetSnapshot(
                event_cursor=cursor,
                generated_at=NOW,
                repository_commit="a" * 40,
                nodes=[],
            )

    stream = FleetStream(
        FleetEventRepository(sessions, clock=lambda: NOW),
        TelemetryRepository(sessions, clock=lambda: NOW),
        Projection(),
        clock=lambda: NOW,
    )

    async def read_reset() -> str:
        generator = stream.events(0)
        try:
            return await anext(generator)
        finally:
            await generator.aclose()

    frame = asyncio.run(read_reset())
    fields = {
        key: value
        for key, value in (
            line.split(": ", 1)
            for line in frame.splitlines()
            if ": " in line
        )
    }
    data = json.loads(fields["data"])
    assert fields["id"] == "1"
    assert fields["event"] == "fleet-snapshot"
    assert data["reset_reason"] == "missing-telemetry-sample"
    assert data["snapshot"]["event_cursor"] == 1


def test_sqlite_late_sample_dirty_marker_survives_claim_transaction(
    tmp_path,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'maintenance-race.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    race_sessions = sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 8, 15, 12, 3, tzinfo=UTC)
    with race_sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
        session.add_all(
            (
                _raw("initial-race", start + timedelta(seconds=10), sequence=1, cpu=10),
                NodeTelemetryRollupDirty(
                    resolution_seconds=60,
                    node_id=NODE_A,
                    bucket_start=start,
                ),
            )
        )

    claimed = threading.Event()
    allow_maintenance = threading.Event()
    late_write_started = threading.Event()
    role = threading.local()

    def observe_after(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            getattr(role, "value", None) == "maintenance"
            and statement.lstrip().startswith(
                "DELETE FROM node_telemetry_rollup_dirty"
            )
        ):
            claimed.set()
            assert allow_maintenance.wait(timeout=10)

    def observe_before(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            getattr(role, "value", None) == "late"
            and statement.lstrip().startswith("INSERT INTO node_telemetry_samples")
        ):
            late_write_started.set()

    maintenance = telemetry_maintenance.TelemetryMaintenance(
        race_sessions, clock=lambda: NOW
    )
    repository = TelemetryRepository(race_sessions, clock=lambda: NOW)

    def run_maintenance() -> None:
        role.value = "maintenance"
        maintenance.run_once(dirty_limit=1)

    def record_late() -> None:
        role.value = "late"
        repository.record_batch(
            NODE_A,
            (
                _input(
                    sequence=2,
                    observed_at=start + timedelta(seconds=30),
                    cpu=30,
                ),
            ),
        )

    event.listen(engine, "after_cursor_execute", observe_after)
    event.listen(engine, "before_cursor_execute", observe_before)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_maintenance)
            assert claimed.wait(timeout=10)
            second = pool.submit(record_late)
            assert late_write_started.wait(timeout=10)
            allow_maintenance.set()
            first.result(timeout=10)
            second.result(timeout=10)
    finally:
        allow_maintenance.set()
        event.remove(engine, "after_cursor_execute", observe_after)
        event.remove(engine, "before_cursor_execute", observe_before)

    with race_sessions() as session:
        assert session.get(NodeTelemetryRollupDirty, (60, NODE_A, start)) is not None

    maintenance.run_once(dirty_limit=1)
    with race_sessions() as session:
        metric = session.get(
            NodeTelemetryRollupMetric,
            (60, NODE_A, start, "cpu_utilization_percent"),
        )
        assert (metric.sample_count, metric.mean) == (2, 20)
