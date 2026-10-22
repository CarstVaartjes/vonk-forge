"""Bounded, ordered persistence for authenticated node telemetry."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.telemetry import (
    HostResourceSample,
    TelemetryRequest,
    TelemetrySample,
)
from vonk_agent_protocol.wire_model import strict_json_datetime

from .lifecycle.evidence import Residue
from .models import (
    AgentNode,
    NodeInventoryResourceSnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
)
from .observed_run_memory import record_observed_run_memory
from .strict_json import warn_unreadable_once


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize SQL timestamps; SQLite drops timezone metadata on round-trip."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TelemetrySampleView(TelemetrySample):
    """Controller identity and receive time around the single sample contract."""

    id: str
    node_id: str
    received_at: datetime

    _parse_received_at = field_validator("received_at", mode="before")(
        strict_json_datetime
    )


def _canonical_sample(value: TelemetrySample, now: datetime) -> TelemetrySample:
    validated = TelemetrySample.model_validate_json(canonical_message(value))
    if validated.observed_at > now + timedelta(
        seconds=30
    ) or now - validated.observed_at > timedelta(minutes=5):
        raise ValueError("telemetry observation time is outside the accepted window")
    return validated


_SAMPLE_FIELDS = tuple(
    name
    for name in TelemetrySample.model_fields
    if name not in HostResourceSample.model_fields
)


def _view(row: NodeTelemetrySample) -> TelemetrySampleView:
    resources = row.resource_snapshot
    if resources is None or isinstance(resources.host_resources, Residue):
        raise ValueError("telemetry resource evidence is unreadable")
    return TelemetrySampleView.model_validate_json(
        canonical_message(
            {
                **resources.host_resources.model_dump(mode="json"),
                **{name: getattr(row, name) for name in _SAMPLE_FIELDS},
                "observed_at": _stored_utc(row.observed_at),
                "id": row.id,
                "node_id": row.node_id,
                "received_at": _stored_utc(row.received_at),
            }
        )
    )


def _same_sample(row: NodeTelemetrySample, value: TelemetrySample) -> bool:
    return (
        TelemetrySample.model_validate_json(
            canonical_message(
                _view(row).model_dump(
                    mode="json", include=set(TelemetrySample.model_fields)
                )
            )
        )
        == value
    )


def _read_view(row: NodeTelemetrySample) -> TelemetrySampleView | None:
    try:
        return _view(row)
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
        self, node_id: str, samples: Sequence[TelemetrySample]
    ) -> tuple[TelemetrySampleView, ...]:
        # Node identity is authenticated before this boundary; existence is
        # checked while locking its owner. The wire owns batch bounds/order.
        values = TelemetryRequest.model_validate_json(
            canonical_message({"samples": list(samples)})
        ).samples
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
            boot_heads: dict[str, NodeTelemetrySample | None] = {}
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
                    **{name: getattr(value, name) for name in _SAMPLE_FIELDS},
                )
                session.add(row)
                session.flush()
                resources = HostResourceSample.model_validate_json(
                    canonical_message(
                        value.model_dump(
                            mode="json", include=set(HostResourceSample.model_fields)
                        )
                    )
                )
                snapshot = NodeInventoryResourceSnapshot(
                    node_id=node_id,
                    observed_at=value.observed_at,
                    received_at=now,
                    source_sample_id=row.id,
                    host_resources=resources,
                    evidence_digest=hashlib.sha256(
                        canonical_message(
                            {
                                "node_id": node_id,
                                "source_sample_id": row.id,
                                "host_resources": resources,
                            }
                        )
                    ).hexdigest(),
                )
                session.add(snapshot)
                row.resource_snapshot = snapshot
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
            record_observed_run_memory(
                session,
                node_id,
                [
                    (value.observed_at, value.memory_available_bytes)
                    for value in canonical
                ],
            )
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
