"""Durable, bounded telemetry rollup and retention maintenance."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, func, select, tuple_, update
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
    TelemetryMaintenanceState,
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


def _dirty_candidate_statement(
    resolution_seconds: RollupResolution,
    limit: int,
):
    return (
        select(
            NodeTelemetryRollupDirty.resolution_seconds,
            NodeTelemetryRollupDirty.node_id,
            NodeTelemetryRollupDirty.bucket_start,
        )
        .where(NodeTelemetryRollupDirty.resolution_seconds == resolution_seconds)
        .order_by(
            NodeTelemetryRollupDirty.bucket_start,
            NodeTelemetryRollupDirty.node_id,
        )
        .limit(limit)
    )


def _claim_dirty_identities_statement(
    identities: list[tuple[int, str, datetime]],
):
    columns = (
        NodeTelemetryRollupDirty.resolution_seconds,
        NodeTelemetryRollupDirty.node_id,
        NodeTelemetryRollupDirty.bucket_start,
    )
    return (
        delete(NodeTelemetryRollupDirty)
        .where(tuple_(*columns).in_(identities))
        .returning(*columns)
    )


def _lock_nodes(session: Session, node_ids: list[str]) -> None:
    ordered = sorted(set(node_ids))
    if session.connection().dialect.name == "sqlite":
        for node_id in ordered:
            session.execute(
                update(AgentNode)
                .where(AgentNode.node_id == node_id)
                .values(node_id=AgentNode.node_id)
            )
        return
    for chunk in TelemetryMaintenance._chunks(ordered):
        session.scalars(
            select(AgentNode.node_id)
            .where(AgentNode.node_id.in_(chunk))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        ).all()


def _lock_maintenance_state(session: Session) -> RollupResolution:
    """Lock and return the durable global single-slot fairness pointer."""

    if session.connection().dialect.name == "sqlite":
        session.execute(
            update(TelemetryMaintenanceState)
            .where(TelemetryMaintenanceState.singleton_id == 1)
            .values(singleton_id=TelemetryMaintenanceState.singleton_id)
        )
        resolution = session.scalar(
            select(TelemetryMaintenanceState.next_resolution_seconds).where(
                TelemetryMaintenanceState.singleton_id == 1
            )
        )
    else:
        resolution = session.scalar(
            select(TelemetryMaintenanceState.next_resolution_seconds)
            .where(TelemetryMaintenanceState.singleton_id == 1)
            .with_for_update()
        )
    if resolution not in (60, 900):
        raise RuntimeError("telemetry maintenance state singleton is not initialized")
    return resolution


def _bucket_end(session: Session, column, seconds: int):
    if session.connection().dialect.name == "sqlite":
        return func.datetime(column, f"+{seconds} seconds")
    return column + timedelta(seconds=seconds)


def _raw_dirty_exists(session: Session):
    return (
        select(1)
        .where(
            NodeTelemetryRollupDirty.resolution_seconds == 60,
            NodeTelemetryRollupDirty.node_id == NodeTelemetrySample.node_id,
            NodeTelemetryRollupDirty.bucket_start <= NodeTelemetrySample.observed_at,
            NodeTelemetrySample.observed_at
            < _bucket_end(
                session,
                NodeTelemetryRollupDirty.bucket_start,
                60,
            ),
        )
        .correlate(NodeTelemetrySample)
        .exists()
    )


def _raw_candidate_statement(
    session: Session,
    *,
    cutoff: datetime,
    limit: int,
    sample_ids: list[str] | None = None,
):
    statement = select(
        NodeTelemetrySample.id,
        NodeTelemetrySample.node_id,
        NodeTelemetrySample.observed_at,
    ).where(
        NodeTelemetrySample.observed_at < cutoff,
        ~_raw_dirty_exists(session),
    )
    if sample_ids is not None:
        statement = statement.where(NodeTelemetrySample.id.in_(sample_ids))
    return statement.order_by(
        NodeTelemetrySample.observed_at,
        NodeTelemetrySample.node_id,
        NodeTelemetrySample.id,
    ).limit(limit)


def _minute_parent_dirty_exists(session: Session):
    return (
        select(1)
        .where(
            NodeTelemetryRollupDirty.resolution_seconds == 900,
            NodeTelemetryRollupDirty.node_id == NodeTelemetryRollupBucket.node_id,
            NodeTelemetryRollupDirty.bucket_start
            <= NodeTelemetryRollupBucket.bucket_start,
            NodeTelemetryRollupBucket.bucket_start
            < _bucket_end(
                session,
                NodeTelemetryRollupDirty.bucket_start,
                900,
            ),
        )
        .correlate(NodeTelemetryRollupBucket)
        .exists()
    )


def _rollup_candidate_statement(
    session: Session,
    *,
    resolution_seconds: RollupResolution,
    cutoff: datetime,
    limit: int,
    identities: list[tuple[int, str, datetime]] | None = None,
):
    columns = (
        NodeTelemetryRollupBucket.resolution_seconds,
        NodeTelemetryRollupBucket.node_id,
        NodeTelemetryRollupBucket.bucket_start,
    )
    statement = select(*columns).where(
        NodeTelemetryRollupBucket.resolution_seconds == resolution_seconds,
        NodeTelemetryRollupBucket.bucket_start < cutoff,
    )
    if resolution_seconds == 60:
        statement = statement.where(~_minute_parent_dirty_exists(session))
    if identities is not None:
        statement = statement.where(tuple_(*columns).in_(identities))
    return statement.order_by(
        NodeTelemetryRollupBucket.bucket_start,
        NodeTelemetryRollupBucket.node_id,
    ).limit(limit)


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
        self._run_rollups(dirty_limit)
        self._run_retention(now, delete_limit)

    def _run_retention(self, now: datetime, limit: int) -> None:
        events = FleetEventRepository(self._sessions, clock=lambda: now)
        with self._sessions() as session:
            raw_candidates = session.execute(
                _raw_candidate_statement(
                    session,
                    cutoff=now - timedelta(hours=24),
                    limit=limit,
                )
            ).all()
        with self._sessions.begin() as session:
            self._prune_raw(
                session,
                cutoff=now - timedelta(hours=24),
                limit=limit,
                events=events,
                candidates=raw_candidates,
            )
        minute_cutoff = bucket_start(now - timedelta(days=30), 60)
        with self._sessions() as session:
            minute_candidates = session.execute(
                _rollup_candidate_statement(
                    session,
                    resolution_seconds=60,
                    cutoff=minute_cutoff,
                    limit=limit,
                )
            ).all()
        with self._sessions.begin() as session:
            self._prune_rollups(
                session,
                resolution_seconds=60,
                cutoff=minute_cutoff,
                limit=limit,
                candidates=minute_candidates,
            )
        quarter_cutoff = bucket_start(now - timedelta(days=365), 900)
        with self._sessions() as session:
            quarter_candidates = session.execute(
                _rollup_candidate_statement(
                    session,
                    resolution_seconds=900,
                    cutoff=quarter_cutoff,
                    limit=limit,
                )
            ).all()
        with self._sessions.begin() as session:
            self._prune_rollups(
                session,
                resolution_seconds=900,
                cutoff=quarter_cutoff,
                limit=limit,
                candidates=quarter_candidates,
            )
        with self._sessions.begin() as session:
            self._prune_events(session, now=now, limit=limit)

    def _run_rollups(self, dirty_limit: int) -> None:
        if dirty_limit == 1:
            self._run_single_rollup()
            return

        with self._sessions() as session:
            candidates = self._select_dirty_candidates(session, dirty_limit)
        with self._sessions.begin() as session:
            _lock_nodes(session, [identity[1] for identity in candidates])
            dirty = (
                [
                    (resolution_seconds, node_id, _database_utc(start))
                    for resolution_seconds, node_id, start in session.execute(
                        _claim_dirty_identities_statement(candidates)
                    ).all()
                ]
                if candidates
                else []
            )
            dirty.sort(key=lambda identity: (identity[0], identity[2], identity[1]))
            for resolution_seconds, node_id, start in dirty:
                if resolution_seconds == 60:
                    self._recompute_minute(session, node_id, start)
                else:
                    self._recompute_quarter_hour(session, node_id, start)

    def _run_single_rollup(self) -> None:
        """Claim one dirty bucket while coordinating fairness across workers."""

        with self._sessions.begin() as session:
            preferred = _lock_maintenance_state(session)
            candidates = session.execute(_dirty_candidate_statement(preferred, 1)).all()
            if not candidates:
                other: RollupResolution = 900 if preferred == 60 else 60
                candidates = session.execute(_dirty_candidate_statement(other, 1)).all()

            _lock_nodes(session, [identity[1] for identity in candidates])
            dirty = (
                [
                    (resolution_seconds, node_id, _database_utc(start))
                    for resolution_seconds, node_id, start in session.execute(
                        _claim_dirty_identities_statement(candidates)
                    ).all()
                ]
                if candidates
                else []
            )
            dirty.sort(key=lambda identity: (identity[0], identity[2], identity[1]))
            for resolution_seconds, node_id, start in dirty:
                if resolution_seconds == 60:
                    self._recompute_minute(session, node_id, start)
                else:
                    self._recompute_quarter_hour(session, node_id, start)
                session.execute(
                    update(TelemetryMaintenanceState)
                    .where(TelemetryMaintenanceState.singleton_id == 1)
                    .values(
                        next_resolution_seconds=(
                            900 if resolution_seconds == 60 else 60
                        )
                    )
                )

    def _select_dirty_candidates(
        self,
        session: Session,
        limit: int,
    ) -> list[tuple[int, str, datetime]]:
        if limit == 1:
            quotas = ((60, 1), (900, 1))
        else:
            quotas = ((60, (limit + 1) // 2), (900, limit // 2))

        candidates: list[tuple[int, str, datetime]] = []
        for resolution_seconds, quota in quotas:
            rows = session.execute(
                _dirty_candidate_statement(resolution_seconds, quota)
            ).all()
            candidates.extend(
                (resolution, node_id, _database_utc(start))
                for resolution, node_id, start in rows
            )
            if limit == 1 and candidates:
                break
        return candidates

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
                NodeTelemetryRollupMetric.resolution_seconds == resolution_seconds,
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
    def _prune_events(session: Session, *, now: datetime, limit: int) -> None:
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
        candidates: list,
    ) -> None:
        sample_ids = [sample_id for sample_id, _node_id, _observed_at in candidates]
        _lock_nodes(
            session,
            [node_id for _sample_id, node_id, _observed_at in candidates],
        )

        locked_rows = []
        for chunk in TelemetryMaintenance._chunks(sample_ids):
            locked_rows.extend(
                session.execute(
                    select(
                        NodeTelemetrySample.id,
                        NodeTelemetrySample.node_id,
                        NodeTelemetrySample.observed_at,
                    )
                    .where(NodeTelemetrySample.id.in_(chunk))
                    .order_by(
                        NodeTelemetrySample.observed_at,
                        NodeTelemetrySample.node_id,
                        NodeTelemetrySample.id,
                    )
                    .with_for_update(of=NodeTelemetrySample)
                ).all()
            )
        locked_ids = [row.id for row in locked_rows]
        rows = session.execute(
            _raw_candidate_statement(
                session,
                cutoff=cutoff,
                limit=limit,
                sample_ids=locked_ids,
            )
        ).all()
        sample_ids = [sample_id for sample_id, _node_id, _observed_at in rows]

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
        candidates: list,
    ) -> None:
        identities = [
            (resolution, node_id, _database_utc(start))
            for resolution, node_id, start in candidates
        ]
        _lock_nodes(session, [identity[1] for identity in identities])

        columns = (
            NodeTelemetryRollupBucket.resolution_seconds,
            NodeTelemetryRollupBucket.node_id,
            NodeTelemetryRollupBucket.bucket_start,
        )
        locked: list[tuple[int, str, datetime]] = []
        for chunk in TelemetryMaintenance._chunks(identities):
            locked.extend(
                (resolution, node_id, _database_utc(start))
                for resolution, node_id, start in session.execute(
                    select(*columns)
                    .where(tuple_(*columns).in_(chunk))
                    .order_by(
                        NodeTelemetryRollupBucket.bucket_start,
                        NodeTelemetryRollupBucket.node_id,
                    )
                    .with_for_update(of=NodeTelemetryRollupBucket)
                )
            )
        rows = session.execute(
            _rollup_candidate_statement(
                session,
                resolution_seconds=resolution_seconds,
                cutoff=cutoff,
                limit=limit,
                identities=locked,
            )
        ).all()
        identities = [
            (resolution, node_id, _database_utc(start))
            for resolution, node_id, start in rows
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
