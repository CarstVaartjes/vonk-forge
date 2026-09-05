"""Bounded, ordered persistence for authenticated node telemetry."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentNode,
    NodeTelemetryLatest,
    NodeTelemetryRollupBucket,
    NodeTelemetryRollupMetric,
    NodeTelemetrySample,
)
from .telemetry_maintenance import mark_rollup_dirty
from .telemetry_contract import TelemetryMetrics, empty_telemetry_metrics

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_MAX_BYTES = 16 * 1024**4
_MAX_RATE = 1_000_000_000_000_000.0
_MAX_HISTORY_POINTS = 3_000
_MAX_BATCH_SAMPLES = 16
TelemetryResolution = Literal["raw", "minute", "fifteen-minute", "daily"]
_HISTORY_WINDOWS: dict[str, timedelta] = {
    "raw": timedelta(hours=24),
    "minute": timedelta(days=30),
    "fifteen-minute": timedelta(days=365),
    "daily": timedelta(days=365),
}
_ROLLUP_SECONDS: dict[str, int] = {"minute": 60, "fifteen-minute": 900}


def _finite_number(
    value: float | None,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"telemetry {label} is invalid")  # noqa: TRY004
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"telemetry {label} is invalid")


def _bounded_bytes(value: int | None, *, label: str) -> None:
    if value is None:
        return
    if type(value) is not int or not 0 <= value <= _MAX_BYTES:
        raise ValueError(f"telemetry {label} is invalid")


def _capacity_pair(
    total: int | None,
    free: int | None,
    *,
    label: str,
    free_label: str,
) -> None:
    if (total is None) != (free is None):
        raise ValueError(
            f"telemetry {label} values must both be present or both be absent"
        )
    _bounded_bytes(total, label=f"{label} total")
    _bounded_bytes(free, label=free_label)
    if total is not None and free is not None and free > total:
        raise ValueError(f"telemetry {free_label} cannot exceed total")


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize SQL timestamps; SQLite drops timezone metadata on round-trip."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TelemetryDetailsInput:
    accelerator_name: str | None = None
    accelerator_performance_state: str | None = None

    def __post_init__(self) -> None:
        if self.accelerator_name is not None and (
            not isinstance(self.accelerator_name, str)
            or not 1 <= len(self.accelerator_name) <= 256
        ):
            raise ValueError("telemetry accelerator name is invalid")
        if self.accelerator_performance_state is not None and (
            not isinstance(self.accelerator_performance_state, str)
            or not 1 <= len(self.accelerator_performance_state) <= 32
        ):
            raise ValueError("telemetry accelerator performance state is invalid")

    def as_dict(self) -> dict[str, str | None]:
        return {
            "accelerator_name": self.accelerator_name,
            "accelerator_performance_state": self.accelerator_performance_state,
        }


@dataclass(frozen=True, slots=True)
class TelemetrySampleInput:
    boot_id: uuid.UUID
    sequence: int
    observed_at: datetime
    cpu_utilization_percent: float | None
    load_average_1m: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    gpu_utilization_percent: float | None
    gpu_memory_total_bytes: int | None
    gpu_memory_free_bytes: int | None
    temperature_c: float | None
    power_watts: float | None
    network_receive_bytes_per_second: float | None
    network_transmit_bytes_per_second: float | None
    gap_samples: int
    details: TelemetryDetailsInput
    metrics: TelemetryMetrics = field(default_factory=empty_telemetry_metrics)

    def __post_init__(self) -> None:
        if not isinstance(self.boot_id, uuid.UUID) or self.boot_id.int == 0:
            raise ValueError("telemetry boot ID is invalid")
        if (
            type(self.sequence) is not int
            or not 0 <= self.sequence <= _MAX_SIGNED_BIGINT
        ):
            raise ValueError("telemetry sequence is invalid")
        if not isinstance(self.observed_at, datetime):
            raise ValueError("telemetry observation time is invalid")  # noqa: TRY004
        _finite_number(
            self.cpu_utilization_percent,
            label="CPU utilization",
            minimum=0,
            maximum=100,
        )
        _finite_number(
            self.load_average_1m,
            label="load average",
            minimum=0,
            maximum=1_000_000,
        )
        _capacity_pair(
            self.memory_total_bytes,
            self.memory_available_bytes,
            label="memory",
            free_label="memory available",
        )
        _capacity_pair(
            self.disk_total_bytes,
            self.disk_free_bytes,
            label="disk",
            free_label="disk free",
        )
        _finite_number(
            self.gpu_utilization_percent,
            label="GPU utilization",
            minimum=0,
            maximum=100,
        )
        _capacity_pair(
            self.gpu_memory_total_bytes,
            self.gpu_memory_free_bytes,
            label="GPU memory",
            free_label="GPU memory free",
        )
        _finite_number(
            self.temperature_c,
            label="temperature",
            minimum=-100,
            maximum=300,
        )
        _finite_number(
            self.power_watts,
            label="power",
            minimum=0,
            maximum=100_000,
        )
        _finite_number(
            self.network_receive_bytes_per_second,
            label="network receive rate",
            minimum=0,
            maximum=_MAX_RATE,
        )
        _finite_number(
            self.network_transmit_bytes_per_second,
            label="network transmit rate",
            minimum=0,
            maximum=_MAX_RATE,
        )
        if (
            type(self.gap_samples) is not int
            or not 0 <= self.gap_samples <= _MAX_SIGNED_BIGINT
        ):
            raise ValueError("telemetry gap samples is invalid")
        if not isinstance(self.details, TelemetryDetailsInput):
            raise ValueError("telemetry details are invalid")  # noqa: TRY004
        if not isinstance(self.metrics, TelemetryMetrics):
            raise ValueError("telemetry metrics are invalid")  # noqa: TRY004


@dataclass(frozen=True, slots=True)
class TelemetrySampleView:
    id: str
    node_id: str
    boot_id: uuid.UUID
    sequence: int
    observed_at: datetime
    received_at: datetime
    cpu_utilization_percent: float | None
    load_average_1m: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    gpu_utilization_percent: float | None
    gpu_memory_total_bytes: int | None
    gpu_memory_free_bytes: int | None
    temperature_c: float | None
    power_watts: float | None
    network_receive_bytes_per_second: float | None
    network_transmit_bytes_per_second: float | None
    gap_samples: int
    details: TelemetryDetailsInput
    metrics: TelemetryMetrics = field(default_factory=empty_telemetry_metrics)


@dataclass(frozen=True, slots=True)
class TelemetryMetricView:
    count: int
    minimum: float
    mean: float
    maximum: float
    # metric_name is an opaque bounded storage key; history keeps the full
    # identity and provenance with the values for unambiguous consumers.
    key: str | None = None
    scope: str | None = None
    device_id: str | None = None
    process_id: int | None = None
    process_name: str | None = None
    interface_name: str | None = None
    run_id: str | None = None
    unit: str = "unknown"
    source: str = "legacy"
    measurement_kind: str = "measured"
    aggregation: str = "mean"


def _merge_metric_views(left: TelemetryMetricView, right: TelemetryMetricView) -> TelemetryMetricView:
    """Merge rollup values while preserving their series identity."""

    count = left.count + right.count
    minimum = min(left.minimum, right.minimum)
    maximum = max(left.maximum, right.maximum)
    if left.aggregation == "max" or right.aggregation == "max":
        return replace(
            right,
            count=count,
            minimum=minimum,
            mean=maximum,
            maximum=maximum,
            key=right.key or left.key,
            scope=right.scope or left.scope,
            device_id=right.device_id or left.device_id,
            process_id=right.process_id if right.process_id is not None else left.process_id,
            process_name=right.process_name or left.process_name,
            interface_name=right.interface_name or left.interface_name,
            run_id=right.run_id or left.run_id,
            aggregation="max",
        )
    return replace(
        right,
        count=count,
        minimum=minimum,
        mean=(left.mean * left.count + right.mean * right.count) / count,
        maximum=maximum,
        key=right.key or left.key,
        scope=right.scope or left.scope,
        device_id=right.device_id or left.device_id,
        process_id=right.process_id if right.process_id is not None else left.process_id,
        process_name=right.process_name or left.process_name,
        interface_name=right.interface_name or left.interface_name,
        run_id=right.run_id or left.run_id,
        aggregation="mean",
    )


@dataclass(frozen=True, slots=True)
class TelemetryRollupPointView:
    node_id: str
    resolution: Literal["minute", "fifteen-minute", "daily"]
    bucket_start: datetime
    bucket_end: datetime
    source_sample_count: int
    gap_samples: int
    metrics: dict[str, TelemetryMetricView]


def _canonical_sample(
    value: TelemetrySampleInput, now: datetime
) -> TelemetrySampleInput:
    if not isinstance(value, TelemetrySampleInput):
        raise ValueError("telemetry sample is invalid")  # noqa: TRY004
    observed_at = _aware_utc(value.observed_at, label="telemetry observation time")
    if observed_at > now + timedelta(seconds=30) or now - observed_at > timedelta(
        minutes=5
    ):
        raise ValueError("telemetry observation time is outside the accepted window")
    return replace(value, observed_at=observed_at)


def _row_values(value: TelemetrySampleInput) -> dict[str, object]:
    return {
        "boot_id": str(value.boot_id),
        "sequence": value.sequence,
        "observed_at": value.observed_at,
        "cpu_utilization_percent": value.cpu_utilization_percent,
        "load_average_1m": value.load_average_1m,
        "memory_total_bytes": value.memory_total_bytes,
        "memory_available_bytes": value.memory_available_bytes,
        "disk_total_bytes": value.disk_total_bytes,
        "disk_free_bytes": value.disk_free_bytes,
        "gpu_utilization_percent": value.gpu_utilization_percent,
        "gpu_memory_total_bytes": value.gpu_memory_total_bytes,
        "gpu_memory_free_bytes": value.gpu_memory_free_bytes,
        "temperature_c": value.temperature_c,
        "power_watts": value.power_watts,
        "network_receive_bytes_per_second": value.network_receive_bytes_per_second,
        "network_transmit_bytes_per_second": value.network_transmit_bytes_per_second,
        "gap_samples": value.gap_samples,
        "details": value.details.as_dict(),
        "metrics": value.metrics.model_dump(mode="json"),
    }


def _same_sample(row: NodeTelemetrySample, value: TelemetrySampleInput) -> bool:
    for field, expected in _row_values(value).items():
        actual = getattr(row, field)
        if field == "observed_at":
            actual = _stored_utc(actual)
        elif field == "details":
            actual = dict(actual)
        elif field == "metrics":
            actual = dict(actual)
        if actual != expected:
            return False
    return True


def _view(row: NodeTelemetrySample) -> TelemetrySampleView:
    details = dict(row.details)
    metrics = TelemetryMetrics.model_validate(row.metrics or empty_telemetry_metrics())
    return TelemetrySampleView(
        id=row.id,
        node_id=row.node_id,
        boot_id=uuid.UUID(row.boot_id),
        sequence=row.sequence,
        observed_at=_stored_utc(row.observed_at),
        received_at=_stored_utc(row.received_at),
        cpu_utilization_percent=row.cpu_utilization_percent,
        load_average_1m=row.load_average_1m,
        memory_total_bytes=row.memory_total_bytes,
        memory_available_bytes=row.memory_available_bytes,
        disk_total_bytes=row.disk_total_bytes,
        disk_free_bytes=row.disk_free_bytes,
        gpu_utilization_percent=row.gpu_utilization_percent,
        gpu_memory_total_bytes=row.gpu_memory_total_bytes,
        gpu_memory_free_bytes=row.gpu_memory_free_bytes,
        temperature_c=row.temperature_c,
        power_watts=row.power_watts,
        network_receive_bytes_per_second=row.network_receive_bytes_per_second,
        network_transmit_bytes_per_second=row.network_transmit_bytes_per_second,
        gap_samples=row.gap_samples,
        details=TelemetryDetailsInput(
            accelerator_name=details.get("accelerator_name"),
            accelerator_performance_state=details.get("accelerator_performance_state"),
        ),
        metrics=metrics,
    )


class TelemetryRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def record_batch(
        self, node_id: str, samples: Sequence[TelemetrySampleInput]
    ) -> tuple[TelemetrySampleView, ...]:
        if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
            raise ValueError("telemetry node ID is invalid")
        values = tuple(samples)
        if not 1 <= len(values) <= _MAX_BATCH_SAMPLES:
            raise ValueError("telemetry batch must contain between 1 and 16 samples")
        now = _aware_utc(self._clock(), label="telemetry receive time")
        canonical = tuple(_canonical_sample(value, now) for value in values)
        keys = [(value.boot_id, value.sequence) for value in canonical]
        if len(keys) != len(set(keys)):
            raise ValueError("telemetry sample is duplicated")
        last_observed: datetime | None = None
        per_boot: dict[uuid.UUID, int] = {}
        for value in canonical:
            if last_observed is not None and value.observed_at <= last_observed:
                raise ValueError("telemetry observation times must increase")
            prior_sequence = per_boot.get(value.boot_id)
            if prior_sequence is not None and value.sequence <= prior_sequence:
                raise ValueError("telemetry sequences must increase")
            per_boot[value.boot_id] = value.sequence
            last_observed = value.observed_at

        stored: list[NodeTelemetrySample] = []
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == node_id)
                .with_for_update(of=AgentNode)
            )
            if node is None:
                raise ValueError("telemetry node ID is unknown")
            pointer = session.get(NodeTelemetryLatest, node_id)
            latest = (
                None
                if pointer is None
                else session.get(NodeTelemetrySample, pointer.sample_id)
            )
            boot_heads: dict[uuid.UUID, NodeTelemetrySample | None] = {}
            for value in canonical:
                boot_id = str(value.boot_id)
                existing = session.scalar(
                    select(NodeTelemetrySample).where(
                        NodeTelemetrySample.node_id == node_id,
                        NodeTelemetrySample.boot_id == boot_id,
                        NodeTelemetrySample.sequence == value.sequence,
                    )
                )
                if existing is not None:
                    if not _same_sample(existing, value):
                        raise ValueError("telemetry conflicts with stored sample")
                    stored.append(existing)
                    continue
                if value.boot_id not in boot_heads:
                    boot_heads[value.boot_id] = session.scalar(
                        select(NodeTelemetrySample)
                        .where(
                            NodeTelemetrySample.node_id == node_id,
                            NodeTelemetrySample.boot_id == boot_id,
                        )
                        .order_by(
                            NodeTelemetrySample.sequence.desc(),
                            NodeTelemetrySample.observed_at.desc(),
                        )
                        .limit(1)
                    )
                boot_head = boot_heads[value.boot_id]
                if boot_head is not None and value.sequence <= boot_head.sequence:
                    raise ValueError("telemetry sample regresses stored sequence")
                if boot_head is not None and value.observed_at <= _aware_utc(
                    boot_head.observed_at.replace(tzinfo=UTC)
                    if boot_head.observed_at.tzinfo is None
                    else boot_head.observed_at,
                    label="telemetry observation time",
                ):
                    raise ValueError(
                        "telemetry sample regresses stored observation time"
                    )
                row = NodeTelemetrySample(
                    node_id=node_id,
                    received_at=now,
                    **_row_values(value),
                )
                session.add(row)
                session.flush()
                mark_rollup_dirty(
                    session,
                    60,
                    node_id,
                    value.observed_at,
                )
                stored.append(row)
                boot_heads[value.boot_id] = row
                if latest is None:
                    advances = True
                elif latest.boot_id == boot_id:
                    advances = (
                        value.sequence > latest.sequence
                        and value.observed_at > _stored_utc(latest.observed_at)
                    )
                else:
                    advances = value.observed_at > _stored_utc(latest.observed_at)
                if advances:
                    if pointer is None:
                        pointer = NodeTelemetryLatest(node_id=node_id, sample_id=row.id)
                        session.add(pointer)
                    else:
                        pointer.sample_id = row.id
                    latest = row
        return tuple(_view(row) for row in stored)

    def latest(self, node_ids: Sequence[str]) -> dict[str, TelemetrySampleView]:
        identities = tuple(dict.fromkeys(node_ids))
        if not identities:
            return {}
        with self._sessions() as session:
            return self.latest_in_session(session, identities)

    def latest_in_session(
        self, session: Session, node_ids: Sequence[str]
    ) -> dict[str, TelemetrySampleView]:
        """Read latest pointers in a caller-owned bounded read transaction."""

        identities = tuple(dict.fromkeys(node_ids))
        if not identities:
            return {}
        rows = session.scalars(
            select(NodeTelemetrySample)
            .join(
                NodeTelemetryLatest,
                NodeTelemetryLatest.sample_id == NodeTelemetrySample.id,
            )
            .where(NodeTelemetryLatest.node_id.in_(identities))
        ).all()
        return {row.node_id: _view(row) for row in rows}

    def by_ids(self, sample_ids: Sequence[str]) -> dict[str, TelemetrySampleView]:
        """Hydrate one bounded stream batch without per-event reads."""

        identities = tuple(dict.fromkeys(sample_ids))
        if not identities:
            return {}
        if len(identities) > 128:
            raise ValueError("telemetry hydration batch exceeds 128 samples")
        with self._sessions() as session:
            rows = session.scalars(
                select(NodeTelemetrySample).where(
                    NodeTelemetrySample.id.in_(identities)
                )
            ).all()
        return {row.id: _view(row) for row in rows}

    def history(
        self,
        node_id: str,
        start: datetime,
        end: datetime,
        maximum_points: int,
        *,
        resolution: TelemetryResolution,
    ) -> tuple[TelemetrySampleView | TelemetryRollupPointView, ...]:
        if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
            raise ValueError("telemetry history node ID is invalid")
        if (
            type(maximum_points) is not int
            or not 1 <= maximum_points <= _MAX_HISTORY_POINTS
        ):
            raise ValueError("telemetry history maximum points is invalid")
        start_utc = _aware_utc(start, label="telemetry history start")
        end_utc = _aware_utc(end, label="telemetry history end")
        if start_utc >= end_utc:
            raise ValueError("telemetry history window is invalid")
        if resolution not in _HISTORY_WINDOWS:
            raise ValueError("telemetry history resolution is invalid")
        if end_utc - start_utc > _HISTORY_WINDOWS[resolution]:
            window = {
                "raw": "24 hours",
                "minute": "30 days",
                "fifteen-minute": "365 days",
                "daily": "365 days",
            }[resolution]
            raise ValueError(f"telemetry history {resolution} window exceeds {window}")
        if resolution == "daily":
            return self._daily_history(
                node_id,
                start_utc,
                end_utc,
                maximum_points,
            )
        with self._sessions() as session:
            if resolution == "raw":
                rows = session.scalars(
                    select(NodeTelemetrySample)
                    .where(
                        NodeTelemetrySample.node_id == node_id,
                        NodeTelemetrySample.observed_at >= start_utc,
                        NodeTelemetrySample.observed_at < end_utc,
                    )
                    .order_by(
                        NodeTelemetrySample.observed_at.desc(),
                        NodeTelemetrySample.sequence.desc(),
                        NodeTelemetrySample.id.desc(),
                    )
                    .limit(maximum_points)
                ).all()
                rows.reverse()
                return tuple(_view(row) for row in rows)

            resolution_seconds = _ROLLUP_SECONDS[resolution]
            buckets = session.scalars(
                select(NodeTelemetryRollupBucket)
                .where(
                    NodeTelemetryRollupBucket.resolution_seconds == resolution_seconds,
                    NodeTelemetryRollupBucket.node_id == node_id,
                    NodeTelemetryRollupBucket.bucket_start >= start_utc,
                    NodeTelemetryRollupBucket.bucket_start < end_utc,
                )
                .order_by(NodeTelemetryRollupBucket.bucket_start.desc())
                .limit(maximum_points)
            ).all()
            buckets.reverse()
            starts = [_stored_utc(row.bucket_start) for row in buckets]
            metrics_by_bucket: dict[datetime, dict[str, TelemetryMetricView]] = {
                start: {} for start in starts
            }
            if starts:
                metric_rows = session.scalars(
                    select(NodeTelemetryRollupMetric)
                    .where(
                        NodeTelemetryRollupMetric.resolution_seconds
                        == resolution_seconds,
                        NodeTelemetryRollupMetric.node_id == node_id,
                        NodeTelemetryRollupMetric.bucket_start.in_(starts),
                    )
                    .order_by(
                        NodeTelemetryRollupMetric.bucket_start,
                        NodeTelemetryRollupMetric.metric_name,
                    )
                ).all()
                for metric in metric_rows:
                    metrics_by_bucket[_stored_utc(metric.bucket_start)][
                        metric.metric_name
                    ] = TelemetryMetricView(
                        count=int(metric.sample_count),
                        minimum=float(metric.minimum),
                        mean=float(metric.mean),
                        maximum=float(metric.maximum),
                        key=metric.key or metric.metric_name,
                        scope=metric.scope,
                        device_id=metric.device_id,
                        process_id=(
                            None if metric.process_id is None else int(metric.process_id)
                        ),
                        process_name=metric.process_name,
                        interface_name=metric.interface_name,
                        run_id=metric.run_id,
                        unit=metric.unit,
                        source=metric.source,
                        measurement_kind=metric.measurement_kind,
                        aggregation=metric.aggregation,
                    )
            return tuple(
                TelemetryRollupPointView(
                    node_id=node_id,
                    resolution=resolution,
                    bucket_start=_stored_utc(bucket.bucket_start),
                    bucket_end=_stored_utc(bucket.bucket_start)
                    + timedelta(seconds=resolution_seconds),
                    source_sample_count=int(bucket.source_sample_count),
                    gap_samples=int(bucket.gap_samples),
                    metrics=metrics_by_bucket[_stored_utc(bucket.bucket_start)],
                )
                for bucket in buckets
            )

    def _daily_history(
        self,
        node_id: str,
        start: datetime,
        end: datetime,
        maximum_points: int,
    ) -> tuple[TelemetryRollupPointView, ...]:
        """Aggregate bounded 15-minute rollups into UTC-local calendar days."""

        # A year contains at most 35,040 quarter-hour buckets.  Cap the
        # source scan so a large point request cannot turn an export into an
        # unbounded JSON/SQL operation; the requested day count remains the
        # response bound.
        source_limit = min(maximum_points * 96, 35_040)
        with self._sessions() as session:
            buckets = session.scalars(
                select(NodeTelemetryRollupBucket)
                .where(
                    NodeTelemetryRollupBucket.resolution_seconds == 900,
                    NodeTelemetryRollupBucket.node_id == node_id,
                    NodeTelemetryRollupBucket.bucket_start >= start,
                    NodeTelemetryRollupBucket.bucket_start < end,
                )
                .order_by(NodeTelemetryRollupBucket.bucket_start.desc())
                .limit(source_limit)
            ).all()
            buckets.reverse()
            starts = [_stored_utc(row.bucket_start) for row in buckets]
            metrics_by_bucket: dict[datetime, dict[str, TelemetryMetricView]] = {
                value: {} for value in starts
            }
            if starts:
                metric_rows = session.scalars(
                    select(NodeTelemetryRollupMetric)
                    .where(
                        NodeTelemetryRollupMetric.resolution_seconds == 900,
                        NodeTelemetryRollupMetric.node_id == node_id,
                        NodeTelemetryRollupMetric.bucket_start.in_(starts),
                    )
                    .order_by(
                        NodeTelemetryRollupMetric.bucket_start,
                        NodeTelemetryRollupMetric.metric_name,
                    )
                ).all()
                for metric in metric_rows:
                    metrics_by_bucket[_stored_utc(metric.bucket_start)][
                        metric.metric_name
                    ] = TelemetryMetricView(
                        count=int(metric.sample_count),
                        minimum=float(metric.minimum),
                        mean=float(metric.mean),
                        maximum=float(metric.maximum),
                        key=metric.key or metric.metric_name,
                        scope=metric.scope,
                        device_id=metric.device_id,
                        process_id=(
                            None if metric.process_id is None else int(metric.process_id)
                        ),
                        process_name=metric.process_name,
                        interface_name=metric.interface_name,
                        run_id=metric.run_id,
                        unit=metric.unit,
                        source=metric.source,
                        measurement_kind=metric.measurement_kind,
                        aggregation=metric.aggregation,
                    )

        grouped: dict[
            datetime,
            tuple[int, int, dict[str, TelemetryMetricView]],
        ] = {}
        for bucket in buckets:
            start_of_day = _stored_utc(bucket.bucket_start).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            source_count, gaps, metrics = grouped.get(
                start_of_day, (0, 0, {})
            )
            source_count += int(bucket.source_sample_count)
            gaps += int(bucket.gap_samples)
            for name, metric in metrics_by_bucket[
                _stored_utc(bucket.bucket_start)
            ].items():
                prior = metrics.get(name)
                if prior is None:
                    metrics[name] = metric
                else:
                    metrics[name] = _merge_metric_views(prior, metric)
            grouped[start_of_day] = (source_count, gaps, metrics)

        points: list[TelemetryRollupPointView] = []
        for day in sorted(grouped)[-maximum_points:]:
            source_count, gaps, values = grouped[day]
            points.append(
                TelemetryRollupPointView(
                    node_id=node_id,
                    resolution="daily",
                    bucket_start=day,
                    bucket_end=day + timedelta(days=1),
                    source_sample_count=source_count,
                    gap_samples=gaps,
                    metrics=values,
                )
            )
        return tuple(points)
