"""Durable, bounded telemetry rollup and retention maintenance."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
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
from .telemetry_contract import TelemetryMetrics, TelemetrySeries

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
_SERIES_METRIC_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class _MetricAggregate:
    name: str
    count: int
    minimum: float
    mean: float
    maximum: float
    key: str | None
    scope: str | None
    device_id: str | None
    process_id: int | None
    process_name: str | None
    interface_name: str | None
    run_id: str | None
    unit: str
    source: str
    measurement_kind: str
    aggregation: str


def _series_metric_name(series) -> str:
    """Create a bounded rollup key without dropping device/run identity."""

    # Include every dimension in the digest input.  In particular, two GPU
    # processes on one device are distinct series even when their names match.
    identity = ":".join(
        (
            series.scope,
            series.key,
            series.device_id or "-",
            "pid-" + str(series.process_id) if series.process_id is not None else "-",
            series.interface_name or "-",
            series.run_id or "-",
        )
    )
    name = ".".join(
        part
        for part in (
            series.scope,
            series.device_id,
            f"pid-{series.process_id}" if series.process_id is not None else None,
            series.interface_name,
            series.run_id,
            series.key,
        )
        if part
    )
    name = name.lower().replace("/", "_")
    if _SERIES_METRIC_NAME.fullmatch(name) is not None and len(name) <= 64:
        return name
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{name[:51]}.{digest}"


def _rich_series_metrics(
    rows: list[NodeTelemetrySample],
) -> list[_MetricAggregate]:
    """Aggregate only available finite numeric series for one minute bucket."""

    values: dict[str, tuple[TelemetrySeries, list[float]]] = {}
    for row in rows:
        try:
            payload = TelemetryMetrics.model_validate(row.metrics or {})
        except (TypeError, ValueError):
            # A historical scalar-only row or a malformed pre-contract row
            # must not make maintenance suppress the valid scalar metrics.
            continue
        for series in payload.series:
            if series.support_status != "available":
                continue
            value = series.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            value = float(value)
            if math.isfinite(value):
                name = _series_metric_name(series)
                current = values.get(name)
                if current is None:
                    values[name] = (series, [value])
                else:
                    # A stable identity should make this impossible, but do
                    # not merge incompatible metadata if a future producer
                    # changes the identity rules.
                    current[1].append(value)
    aggregates: list[_MetricAggregate] = []
    for name, (series, items) in sorted(values.items()):
        is_percentile = "p95" in series.key or "p95" in series.aggregation
        maximum = max(items)
        minimum = min(items)
        aggregates.append(
            _MetricAggregate(
                name=name,
                count=len(items),
                minimum=minimum,
                mean=maximum if is_percentile else sum(items) / len(items),
                maximum=maximum,
                key=series.key,
                scope=series.scope,
                device_id=series.device_id,
                process_id=series.process_id,
                process_name=series.process_name,
                interface_name=series.interface_name,
                run_id=series.run_id,
                unit=series.unit,
                source=series.source,
                measurement_kind=series.measurement_kind,
                # A sampled percentile cannot be averaged.  Expose the
                # conservative max operation used for its rollup.
                aggregation="max" if is_percentile else "mean",
            )
        )
    return aggregates


def _scalar_metric(
    name: str,
    sample_count: int,
    minimum: float,
    mean: float,
    maximum: float,
) -> _MetricAggregate:
    metadata = {
        "cpu_utilization_percent": ("%", "procfs:/proc/stat", "derived"),
        "load_average_1m": ("load", "procfs:/proc/loadavg", "measured"),
        "memory_total_bytes": ("bytes", "procfs:/proc/meminfo", "measured"),
        "memory_available_bytes": ("bytes", "procfs:/proc/meminfo", "measured"),
        "disk_total_bytes": ("bytes", "statvfs", "measured"),
        "disk_free_bytes": ("bytes", "statvfs", "measured"),
        "gpu_utilization_percent": ("%", "nvidia-smi", "measured"),
        "gpu_memory_total_bytes": ("bytes", "nvidia-smi", "measured"),
        "gpu_memory_free_bytes": ("bytes", "nvidia-smi", "measured"),
        "temperature_c": ("degC", "nvidia-smi", "measured"),
        "power_watts": ("W", "nvidia-smi", "measured"),
        "network_receive_bytes_per_second": (
            "bytes/s",
            "procfs:/proc/net/dev",
            "derived",
        ),
        "network_transmit_bytes_per_second": (
            "bytes/s",
            "procfs:/proc/net/dev",
            "derived",
        ),
    }
    unit, source, measurement_kind = metadata.get(
        name, ("unknown", "legacy", "measured")
    )
    return _MetricAggregate(
        name=name,
        count=sample_count,
        minimum=minimum,
        mean=mean,
        maximum=maximum,
        key=name,
        scope="node",
        device_id=None,
        process_id=None,
        process_name=None,
        interface_name=None,
        run_id=None,
        unit=unit,
        source=source,
        measurement_kind=measurement_kind,
        aggregation="mean",
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


def _clamp_rollup_mean(minimum: float, mean: float, maximum: float) -> float:
    """Keep floating-point aggregate error inside the observed value bounds."""

    if minimum > maximum:
        raise RuntimeError("telemetry rollup minimum exceeds maximum")
    return max(minimum, min(mean, maximum))


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
        rich_rows = session.scalars(
            select(NodeTelemetrySample).where(
                NodeTelemetrySample.node_id == node_id,
                NodeTelemetrySample.observed_at >= start,
                NodeTelemetrySample.observed_at < end,
            )
        ).all()
        source_sample_count = int(row[0])
        gap_samples = int(row[1])
        metrics: list[_MetricAggregate] = []
        offset = 2
        for name, _column in _METRICS:
            sample_count = int(row[offset])
            if sample_count:
                metrics.append(
                    _scalar_metric(
                        name,
                        sample_count,
                        float(row[offset + 1]),
                        float(row[offset + 2]),
                        float(row[offset + 3]),
                    )
                )
            offset += 4
        metrics.extend(_rich_series_metrics(rich_rows))
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
        metric_rows = session.scalars(
            select(NodeTelemetryRollupMetric)
            .where(
                NodeTelemetryRollupMetric.resolution_seconds == 60,
                NodeTelemetryRollupMetric.node_id == node_id,
                NodeTelemetryRollupMetric.bucket_start >= start,
                NodeTelemetryRollupMetric.bucket_start < end,
            )
            .order_by(
                NodeTelemetryRollupMetric.metric_name,
                NodeTelemetryRollupMetric.bucket_start,
            )
        ).all()
        aggregates: dict[tuple[object, ...], _MetricAggregate] = {}
        for metric in metric_rows:
            if not metric.sample_count:
                continue
            identity = (
                metric.metric_name,
                metric.key,
                metric.scope,
                metric.device_id,
                metric.process_id,
                metric.process_name,
                metric.interface_name,
                metric.run_id,
                metric.unit,
                metric.source,
                metric.measurement_kind,
                metric.aggregation,
            )
            current = aggregates.get(identity)
            incoming = _MetricAggregate(
                name=metric.metric_name,
                count=int(metric.sample_count),
                minimum=float(metric.minimum),
                mean=float(metric.mean),
                maximum=float(metric.maximum),
                key=metric.key,
                scope=metric.scope,
                device_id=metric.device_id,
                process_id=(None if metric.process_id is None else int(metric.process_id)),
                process_name=metric.process_name,
                interface_name=metric.interface_name,
                run_id=metric.run_id,
                unit=metric.unit,
                source=metric.source,
                measurement_kind=metric.measurement_kind,
                aggregation=metric.aggregation,
            )
            if current is None:
                aggregates[identity] = incoming
            elif current.aggregation == "max" or incoming.aggregation == "max":
                high = max(current.maximum, incoming.maximum)
                aggregates[identity] = replace(
                    current,
                    count=current.count + incoming.count,
                    minimum=min(current.minimum, incoming.minimum),
                    mean=high,
                    maximum=high,
                    aggregation="max",
                )
            else:
                count = current.count + incoming.count
                aggregates[identity] = replace(
                    current,
                    count=count,
                    minimum=min(current.minimum, incoming.minimum),
                    mean=(
                        current.mean * current.count + incoming.mean * incoming.count
                    )
                    / count,
                    maximum=max(current.maximum, incoming.maximum),
                )
        metrics = sorted(aggregates.values(), key=lambda metric: metric.name)
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
        metrics: list[_MetricAggregate],
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
                metric_name=metric.name,
                key=metric.key,
                scope=metric.scope,
                device_id=metric.device_id,
                process_id=metric.process_id,
                process_name=metric.process_name,
                interface_name=metric.interface_name,
                run_id=metric.run_id,
                unit=metric.unit,
                source=metric.source,
                measurement_kind=metric.measurement_kind,
                aggregation=metric.aggregation,
                sample_count=metric.count,
                minimum=metric.minimum,
                mean=_clamp_rollup_mean(metric.minimum, metric.mean, metric.maximum),
                maximum=metric.maximum,
            )
            for metric in metrics
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
