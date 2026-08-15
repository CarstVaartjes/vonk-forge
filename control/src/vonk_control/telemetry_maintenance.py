"""Durable, bounded telemetry rollup and retention maintenance."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .fleet_events import FleetEventDraft, FleetEventRepository
from .models import (
    AgentNode,
    FleetStreamEvent,
    NodeTelemetryLatest,
    NodeTelemetryRollupBucket,
    NodeTelemetryRollupDirty,
    NodeTelemetryRollupMetric,
    NodeTelemetrySample,
)

RollupResolution = Literal[60, 900]
_MAX_MAINTENANCE_LIMIT = 25_000
_SQL_KEY_CHUNK = 250
_MAINTENANCE_INTERVAL = timedelta(seconds=15)
_METRICS = (
    ("cpu_utilization_percent", NodeTelemetrySample.cpu_utilization_percent),
    ("load_average_1m", NodeTelemetrySample.load_average_1m),
    ("memory_total_bytes", NodeTelemetrySample.memory_total_bytes),
    ("memory_available_bytes", NodeTelemetrySample.memory_available_bytes),
    ("disk_total_bytes", NodeTelemetrySample.disk_total_bytes),
    ("disk_free_bytes", NodeTelemetrySample.disk_free_bytes),
    ("gpu_utilization_percent", NodeTelemetrySample.gpu_utilization_percent),
    ("gpu_memory_total_bytes", NodeTelemetrySample.gpu_memory_total_bytes),
    ("gpu_memory_free_bytes", NodeTelemetrySample.gpu_memory_free_bytes),
    ("temperature_c", NodeTelemetrySample.temperature_c),
    ("power_watts", NodeTelemetrySample.power_watts),
    (
        "network_receive_bytes_per_second",
        NodeTelemetrySample.network_receive_bytes_per_second,
    ),
    (
        "network_transmit_bytes_per_second",
        NodeTelemetrySample.network_transmit_bytes_per_second,
    ),
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validated_limit(value: int, *, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_MAINTENANCE_LIMIT:
        raise ValueError(f"{label} must be between 1 and 25000")
    return value


def _claim_dirty_statement(limit: int):
    keys = (
        select(
            NodeTelemetryRollupDirty.resolution_seconds,
            NodeTelemetryRollupDirty.node_id,
            NodeTelemetryRollupDirty.bucket_start,
        )
        .order_by(
            NodeTelemetryRollupDirty.resolution_seconds,
            NodeTelemetryRollupDirty.bucket_start,
            NodeTelemetryRollupDirty.node_id,
        )
        .limit(limit)
    )
    return (
        delete(NodeTelemetryRollupDirty)
        .where(
            tuple_(
                NodeTelemetryRollupDirty.resolution_seconds,
                NodeTelemetryRollupDirty.node_id,
                NodeTelemetryRollupDirty.bucket_start,
            ).in_(keys)
        )
        .returning(
            NodeTelemetryRollupDirty.resolution_seconds,
            NodeTelemetryRollupDirty.node_id,
            NodeTelemetryRollupDirty.bucket_start,
        )
    )


def bucket_start(value: datetime, resolution_seconds: RollupResolution) -> datetime:
    """Floor one aware timestamp to an exact UTC rollup boundary."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("telemetry rollup time must be timezone-aware")
    if resolution_seconds not in (60, 900):
        raise ValueError("telemetry rollup resolution must be 60 or 900 seconds")
    utc = value.astimezone(UTC)
    minute = utc.minute if resolution_seconds == 60 else (utc.minute // 15) * 15
    return utc.replace(minute=minute, second=0, microsecond=0)


def mark_rollup_dirty(
    session: Session,
    resolution_seconds: RollupResolution,
    node_id: str,
    start: datetime,
) -> None:
    """Idempotently enqueue one rollup identity in the caller's transaction."""

    values = {
        "resolution_seconds": resolution_seconds,
        "node_id": node_id,
        "bucket_start": bucket_start(start, resolution_seconds),
    }
    dialect = session.connection().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(NodeTelemetryRollupDirty)
    elif dialect == "postgresql":
        statement = postgresql_insert(NodeTelemetryRollupDirty)
    else:
        raise RuntimeError(f"unsupported telemetry maintenance dialect: {dialect}")
    session.execute(statement.values(**values).on_conflict_do_nothing())


class TelemetryMaintenance:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def run_once(
        self,
        dirty_limit: int = 512,
        delete_limit: int = 25_000,
    ) -> None:
        dirty_limit = _validated_limit(dirty_limit, label="dirty limit")
        delete_limit = _validated_limit(delete_limit, label="delete limit")
        now = _aware_utc(self._clock(), label="telemetry maintenance clock")
        with self._sessions.begin() as session:
            dirty = [
                (resolution_seconds, node_id, _database_utc(start))
                for resolution_seconds, node_id, start in session.execute(
                    _claim_dirty_statement(dirty_limit)
                ).all()
            ]
            dirty.sort(key=lambda identity: (identity[0], identity[2], identity[1]))
            for resolution_seconds, node_id, start in dirty:
                if resolution_seconds == 60:
                    self._recompute_minute(session, node_id, start)
                else:
                    self._recompute_quarter_hour(session, node_id, start)
            self._prune(
                session,
                now,
                delete_limit,
                FleetEventRepository(self._sessions, clock=lambda: now),
            )

    @staticmethod
    def _recompute_minute(
        session: Session,
        node_id: str,
        start: datetime,
    ) -> None:
        end = start + timedelta(seconds=60)
        aggregates = [
            func.count(NodeTelemetrySample.id),
            func.coalesce(func.sum(NodeTelemetrySample.gap_samples), 0),
        ]
        for _name, column in _METRICS:
            aggregates.extend(
                (
                    func.count(column),
                    func.min(column),
                    func.avg(column),
                    func.max(column),
                )
            )
        row = session.execute(
            select(*aggregates).where(
                NodeTelemetrySample.node_id == node_id,
                NodeTelemetrySample.observed_at >= start,
                NodeTelemetrySample.observed_at < end,
            )
        ).one()
        source_sample_count = int(row[0])
        gap_samples = int(row[1])
        metrics: list[tuple[str, int, float, float, float]] = []
        offset = 2
        for name, _column in _METRICS:
            sample_count = int(row[offset])
            if sample_count:
                metrics.append(
                    (
                        name,
                        sample_count,
                        float(row[offset + 1]),
                        float(row[offset + 2]),
                        float(row[offset + 3]),
                    )
                )
            offset += 4
        TelemetryMaintenance._replace_bucket(
            session,
            resolution_seconds=60,
            node_id=node_id,
            start=start,
            source_sample_count=source_sample_count,
            gap_samples=gap_samples,
            metrics=metrics,
        )
        mark_rollup_dirty(session, 900, node_id, start)

    @staticmethod
    def _recompute_quarter_hour(
        session: Session,
        node_id: str,
        start: datetime,
    ) -> None:
        end = start + timedelta(seconds=900)
        source_sample_count, gap_samples = session.execute(
            select(
                func.coalesce(
                    func.sum(NodeTelemetryRollupBucket.source_sample_count), 0
                ),
                func.coalesce(func.sum(NodeTelemetryRollupBucket.gap_samples), 0),
            ).where(
                NodeTelemetryRollupBucket.resolution_seconds == 60,
                NodeTelemetryRollupBucket.node_id == node_id,
                NodeTelemetryRollupBucket.bucket_start >= start,
                NodeTelemetryRollupBucket.bucket_start < end,
            )
        ).one()
        metric_rows = session.execute(
            select(
                NodeTelemetryRollupMetric.metric_name,
                func.sum(NodeTelemetryRollupMetric.sample_count),
                func.min(NodeTelemetryRollupMetric.minimum),
                func.sum(
                    NodeTelemetryRollupMetric.mean
                    * NodeTelemetryRollupMetric.sample_count
                )
                / func.sum(NodeTelemetryRollupMetric.sample_count),
                func.max(NodeTelemetryRollupMetric.maximum),
            )
            .where(
                NodeTelemetryRollupMetric.resolution_seconds == 60,
                NodeTelemetryRollupMetric.node_id == node_id,
                NodeTelemetryRollupMetric.bucket_start >= start,
                NodeTelemetryRollupMetric.bucket_start < end,
            )
            .group_by(NodeTelemetryRollupMetric.metric_name)
            .order_by(NodeTelemetryRollupMetric.metric_name)
        ).all()
        metrics = [
            (
                name,
                int(sample_count),
                float(minimum),
                float(mean),
                float(maximum),
            )
            for name, sample_count, minimum, mean, maximum in metric_rows
            if sample_count
        ]
        TelemetryMaintenance._replace_bucket(
            session,
            resolution_seconds=900,
            node_id=node_id,
            start=start,
            source_sample_count=int(source_sample_count),
            gap_samples=int(gap_samples),
            metrics=metrics,
        )

    @staticmethod
    def _replace_bucket(
        session: Session,
        *,
        resolution_seconds: RollupResolution,
        node_id: str,
        start: datetime,
        source_sample_count: int,
        gap_samples: int,
        metrics: list[tuple[str, int, float, float, float]],
    ) -> None:
        identity = (resolution_seconds, node_id, start)
        session.execute(
            delete(NodeTelemetryRollupMetric).where(
                NodeTelemetryRollupMetric.resolution_seconds
                == resolution_seconds,
                NodeTelemetryRollupMetric.node_id == node_id,
                NodeTelemetryRollupMetric.bucket_start == start,
            )
        )
        bucket = session.get(NodeTelemetryRollupBucket, identity)
        if source_sample_count == 0:
            if bucket is not None:
                session.delete(bucket)
            return
        if bucket is None:
            bucket = NodeTelemetryRollupBucket(
                resolution_seconds=resolution_seconds,
                node_id=node_id,
                bucket_start=start,
                source_sample_count=source_sample_count,
                gap_samples=gap_samples,
            )
            session.add(bucket)
        else:
            bucket.source_sample_count = source_sample_count
            bucket.gap_samples = gap_samples
        session.add_all(
            NodeTelemetryRollupMetric(
                resolution_seconds=resolution_seconds,
                node_id=node_id,
                bucket_start=start,
                metric_name=name,
                sample_count=sample_count,
                minimum=minimum,
                mean=mean,
                maximum=maximum,
            )
            for name, sample_count, minimum, mean, maximum in metrics
        )

    @staticmethod
    def _prune(
        session: Session,
        now: datetime,
        limit: int,
        events: FleetEventRepository,
    ) -> None:
        TelemetryMaintenance._prune_raw(
            session,
            cutoff=now - timedelta(hours=24),
            limit=limit,
            events=events,
        )
        TelemetryMaintenance._prune_rollups(
            session,
            resolution_seconds=60,
            cutoff=bucket_start(now - timedelta(days=30), 60),
            limit=limit,
            protect_parent=True,
        )
        TelemetryMaintenance._prune_rollups(
            session,
            resolution_seconds=900,
            cutoff=bucket_start(now - timedelta(days=365), 900),
            limit=limit,
            protect_parent=False,
        )
        event_ids = list(
            session.scalars(
                select(FleetStreamEvent.id)
                .where(FleetStreamEvent.expires_at <= now)
                .order_by(FleetStreamEvent.expires_at, FleetStreamEvent.id)
                .limit(limit)
            )
        )
        for chunk in TelemetryMaintenance._chunks(event_ids):
            session.execute(
                delete(FleetStreamEvent)
                .where(FleetStreamEvent.id.in_(chunk))
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    def _prune_raw(
        session: Session,
        *,
        cutoff: datetime,
        limit: int,
        events: FleetEventRepository,
    ) -> None:
        rows = session.execute(
            select(
                NodeTelemetrySample.id,
                NodeTelemetrySample.node_id,
                NodeTelemetrySample.observed_at,
            )
            .where(NodeTelemetrySample.observed_at < cutoff)
            .order_by(
                NodeTelemetrySample.observed_at,
                NodeTelemetrySample.node_id,
                NodeTelemetrySample.id,
            )
            .limit(limit)
        ).all()
        identities = [
            (60, node_id, bucket_start(_database_utc(observed_at), 60))
            for _sample_id, node_id, observed_at in rows
        ]
        protected = TelemetryMaintenance._dirty_identities(session, identities)
        sample_ids = [
            sample_id
            for sample_id, node_id, observed_at in rows
            if (
                60,
                node_id,
                bucket_start(_database_utc(observed_at), 60),
            )
            not in protected
        ]
        if sample_ids:
            sample_id_set = set(sample_ids)
            node_ids = sorted(
                {
                    node_id
                    for sample_id, node_id, _observed_at in rows
                    if sample_id in sample_id_set
                }
            )
            for chunk in TelemetryMaintenance._chunks(node_ids):
                session.scalars(
                    select(AgentNode.node_id)
                    .where(AgentNode.node_id.in_(chunk))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                ).all()
            pointers: list[NodeTelemetryLatest] = []
            for chunk in TelemetryMaintenance._chunks(sample_ids):
                pointers.extend(
                    session.scalars(
                        select(NodeTelemetryLatest)
                        .where(NodeTelemetryLatest.sample_id.in_(chunk))
                        .order_by(NodeTelemetryLatest.node_id)
                        .with_for_update(of=NodeTelemetryLatest)
                    ).all()
                )
            pointers.sort(key=lambda pointer: pointer.node_id)
            for pointer in pointers:
                events.append_in_session(
                    session,
                    FleetEventDraft(
                        event_type="node-telemetry",
                        node_id=pointer.node_id,
                        entity_kind="node-telemetry-latest",
                        entity_id=pointer.node_id,
                        payload={
                            "schema_version": 1,
                            "node_id": pointer.node_id,
                            "sample_id": pointer.sample_id,
                        },
                    ),
                )
                session.delete(pointer)
            session.flush()
        for chunk in TelemetryMaintenance._chunks(sample_ids):
            session.execute(
                delete(NodeTelemetrySample)
                .where(NodeTelemetrySample.id.in_(chunk))
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    def _prune_rollups(
        session: Session,
        *,
        resolution_seconds: RollupResolution,
        cutoff: datetime,
        limit: int,
        protect_parent: bool,
    ) -> None:
        rows = session.execute(
            select(
                NodeTelemetryRollupBucket.resolution_seconds,
                NodeTelemetryRollupBucket.node_id,
                NodeTelemetryRollupBucket.bucket_start,
            )
            .where(
                NodeTelemetryRollupBucket.resolution_seconds
                == resolution_seconds,
                NodeTelemetryRollupBucket.bucket_start < cutoff,
            )
            .order_by(
                NodeTelemetryRollupBucket.bucket_start,
                NodeTelemetryRollupBucket.node_id,
            )
            .limit(limit)
        ).all()
        identities = [
            (resolution, node_id, _database_utc(start))
            for resolution, node_id, start in rows
        ]
        if protect_parent:
            parent_by_identity = {
                identity: (900, identity[1], bucket_start(identity[2], 900))
                for identity in identities
            }
            protected_parents = TelemetryMaintenance._dirty_identities(
                session, list(parent_by_identity.values())
            )
            identities = [
                identity
                for identity in identities
                if parent_by_identity[identity] not in protected_parents
            ]
        TelemetryMaintenance._delete_bucket_identities(session, identities)

    @staticmethod
    def _delete_bucket_identities(
        session: Session,
        identities: list[tuple[int, str, datetime]],
    ) -> None:
        columns = (
            NodeTelemetryRollupBucket.resolution_seconds,
            NodeTelemetryRollupBucket.node_id,
            NodeTelemetryRollupBucket.bucket_start,
        )
        metric_columns = (
            NodeTelemetryRollupMetric.resolution_seconds,
            NodeTelemetryRollupMetric.node_id,
            NodeTelemetryRollupMetric.bucket_start,
        )
        for chunk in TelemetryMaintenance._chunks(identities):
            session.execute(
                delete(NodeTelemetryRollupMetric)
                .where(tuple_(*metric_columns).in_(chunk))
                .execution_options(synchronize_session=False)
            )
            session.execute(
                delete(NodeTelemetryRollupBucket)
                .where(tuple_(*columns).in_(chunk))
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    def _dirty_identities(
        session: Session,
        identities: list[tuple[int, str, datetime]],
    ) -> set[tuple[int, str, datetime]]:
        if not identities:
            return set()
        columns = (
            NodeTelemetryRollupDirty.resolution_seconds,
            NodeTelemetryRollupDirty.node_id,
            NodeTelemetryRollupDirty.bucket_start,
        )
        found: set[tuple[int, str, datetime]] = set()
        for chunk in TelemetryMaintenance._chunks(list(dict.fromkeys(identities))):
            found.update(
                (resolution, node_id, _database_utc(start))
                for resolution, node_id, start in session.execute(
                    select(*columns).where(tuple_(*columns).in_(chunk))
                )
            )
        return found

    @staticmethod
    def _chunks(values: list, size: int = _SQL_KEY_CHUNK):
        for offset in range(0, len(values), size):
            yield values[offset : offset + size]


class TelemetryMaintenanceCadence:
    """Run one bounded maintenance transaction on a fixed 15-second cadence."""

    def __init__(
        self,
        maintenance: TelemetryMaintenance,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._maintenance = maintenance
        self._clock = clock
        self._next_due_at: datetime | None = None

    def __call__(self) -> None:
        now = _aware_utc(self._clock(), label="telemetry maintenance cadence clock")
        if self._next_due_at is not None and now < self._next_due_at:
            return
        if self._next_due_at is None:
            self._next_due_at = now + _MAINTENANCE_INTERVAL
        else:
            elapsed = now - self._next_due_at
            intervals = int(elapsed.total_seconds() // 15) + 1
            self._next_due_at += _MAINTENANCE_INTERVAL * intervals
        self._maintenance.run_once()
