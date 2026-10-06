"""Bounded, ordered persistence for authenticated node telemetry."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import AgentNode, NodeTelemetryLatest, NodeTelemetrySample
from .strict_json import warn_unreadable_once

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_MAX_BYTES = 16 * 1024**4
_MAX_BATCH_SAMPLES = 16


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
class TelemetrySampleInput:
    boot_id: uuid.UUID
    observed_at: datetime
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    gpu_utilization_percent: float | None
    gpu_memory_total_bytes: int | None
    gpu_memory_free_bytes: int | None
    gpu_temperature_c: int | None = None
    cpu_frequency_avg_mhz: int | None = None
    cpu_frequency_min_mhz: int | None = None
    cpu_frequency_max_mhz: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.boot_id, uuid.UUID) or self.boot_id.int == 0:
            raise ValueError("telemetry boot ID is invalid")
        if not isinstance(self.observed_at, datetime):
            raise ValueError("telemetry observation time is invalid")  # noqa: TRY004
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
            self.gpu_temperature_c, label="GPU temperature", minimum=0, maximum=150
        )
        for label, value in (
            ("average CPU frequency", self.cpu_frequency_avg_mhz),
            ("minimum CPU frequency", self.cpu_frequency_min_mhz),
            ("maximum CPU frequency", self.cpu_frequency_max_mhz),
        ):
            _finite_number(value, label=label, minimum=1, maximum=20_000)


@dataclass(frozen=True, slots=True)
class TelemetrySampleView:
    id: str
    node_id: str
    boot_id: uuid.UUID
    observed_at: datetime
    received_at: datetime
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    gpu_utilization_percent: float | None
    gpu_memory_total_bytes: int | None
    gpu_memory_free_bytes: int | None
    gpu_temperature_c: int | None = None
    cpu_frequency_avg_mhz: int | None = None
    cpu_frequency_min_mhz: int | None = None
    cpu_frequency_max_mhz: int | None = None


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


_SAMPLE_FIELDS = (
    "memory_total_bytes",
    "memory_available_bytes",
    "disk_total_bytes",
    "disk_free_bytes",
    "gpu_utilization_percent",
    "gpu_memory_total_bytes",
    "gpu_memory_free_bytes",
    "gpu_temperature_c",
    "cpu_frequency_avg_mhz",
    "cpu_frequency_min_mhz",
    "cpu_frequency_max_mhz",
)


def _row_values(value: TelemetrySampleInput) -> dict[str, object]:
    return {
        "boot_id": str(value.boot_id),
        "observed_at": value.observed_at,
        **{name: getattr(value, name) for name in _SAMPLE_FIELDS},
    }


def _same_sample(row: NodeTelemetrySample, value: TelemetrySampleInput) -> bool:
    for field_name, expected in _row_values(value).items():
        actual = getattr(row, field_name)
        if field_name == "observed_at":
            actual = _stored_utc(actual)
        if actual != expected:
            return False
    return True


def _view(row: NodeTelemetrySample) -> TelemetrySampleView:
    return TelemetrySampleView(
        id=row.id,
        node_id=row.node_id,
        boot_id=uuid.UUID(row.boot_id),
        observed_at=_stored_utc(row.observed_at),
        received_at=_stored_utc(row.received_at),
        **{name: getattr(row, name) for name in _SAMPLE_FIELDS},
    )


def _read_view(row: NodeTelemetrySample) -> TelemetrySampleView | None:
    """Validate stored values without reapplying the ingestion time window."""
    try:
        view = _view(row)
        TelemetrySampleInput(
            boot_id=view.boot_id,
            observed_at=view.observed_at,
            **{name: getattr(view, name) for name in _SAMPLE_FIELDS},
        )
        return view
    except (AttributeError, TypeError, ValueError):
        warn_unreadable_once("telemetry sample", row.id)
        return None


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
        keys = [(value.boot_id, value.observed_at) for value in canonical]
        if len(keys) != len(set(keys)):
            raise ValueError("telemetry sample is duplicated")
        last_observed: datetime | None = None
        for value in canonical:
            if last_observed is not None and value.observed_at <= last_observed:
                raise ValueError("telemetry observation times must increase")
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
                        NodeTelemetrySample.observed_at == value.observed_at,
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
                        .order_by(NodeTelemetrySample.observed_at.desc())
                        .limit(1)
                    )
                boot_head = boot_heads[value.boot_id]
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
                stored.append(row)
                boot_heads[value.boot_id] = row
                if latest is None:
                    advances = True
                elif latest.boot_id == boot_id:
                    advances = value.observed_at > _stored_utc(latest.observed_at)
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
        self,
        session: Session,
        node_ids: Sequence[str],
        *,
        unreadable_node_ids: set[str] | None = None,
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
        readable: dict[str, TelemetrySampleView] = {}
        for row in rows:
            view = _read_view(row)
            if view is None:
                if unreadable_node_ids is not None:
                    unreadable_node_ids.add(row.node_id)
            else:
                readable[row.node_id] = view
        return readable

    def recent_in_session(
        self,
        session: Session,
        node_ids: Sequence[str],
        since: datetime,
    ) -> dict[str, list[TelemetrySampleView]]:
        """Read each node's samples observed at or after ``since``, oldest first."""

        identities = tuple(dict.fromkeys(node_ids))
        if not identities:
            return {}
        rows = session.scalars(
            select(NodeTelemetrySample)
            .where(
                NodeTelemetrySample.node_id.in_(identities),
                NodeTelemetrySample.observed_at >= since,
            )
            .order_by(NodeTelemetrySample.node_id, NodeTelemetrySample.observed_at)
        ).all()
        recent: dict[str, list[TelemetrySampleView]] = {}
        for row in rows:
            view = _read_view(row)
            if view is not None:
                recent.setdefault(row.node_id, []).append(view)
        return recent

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
        return {row.id: view for row in rows if (view := _read_view(row)) is not None}


# Fixed rule for a CPU that runs well below its maximum clock while it is hot or
# busy. It is a hint: only the platform can say why the clock is low.
CPU_LOW_CLOCK_RATIO = 0.7
CPU_LOW_CLOCK_MIN_TEMPERATURE_C = 80
CPU_LOW_CLOCK_MIN_GPU_UTILIZATION_PERCENT = 50.0
CPU_LOW_CLOCK_MIN_SECONDS = 60


def cpu_clock_is_low(sample: TelemetrySampleView) -> bool:
    """One sample: average clock under 70% of max, and hot or loaded."""

    average = sample.cpu_frequency_avg_mhz
    maximum = sample.cpu_frequency_max_mhz
    if average is None or maximum is None or average >= maximum * CPU_LOW_CLOCK_RATIO:
        return False
    return (
        sample.gpu_temperature_c is not None
        and sample.gpu_temperature_c >= CPU_LOW_CLOCK_MIN_TEMPERATURE_C
    ) or (
        sample.gpu_utilization_percent is not None
        and sample.gpu_utilization_percent >= CPU_LOW_CLOCK_MIN_GPU_UTILIZATION_PERCENT
    )


def sustained_low_cpu_clock(
    recent: Sequence[TelemetrySampleView],
) -> TelemetrySampleView | None:
    """Return the newest sample when the clock stayed low for over a minute.

    ``recent`` is oldest first. The unbroken low run ending at the newest sample
    must span more than ``CPU_LOW_CLOCK_MIN_SECONDS``.
    """

    if not recent or not cpu_clock_is_low(recent[-1]):
        return None
    first = recent[-1]
    for sample in reversed(recent):
        if not cpu_clock_is_low(sample):
            break
        first = sample
    span = recent[-1].observed_at - first.observed_at
    if span <= timedelta(seconds=CPU_LOW_CLOCK_MIN_SECONDS):
        return None
    return recent[-1]
