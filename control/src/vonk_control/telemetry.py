"""Bounded, ordered persistence for authenticated node telemetry."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import field_validator
from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import InvalidRequestReason, canonical_message
from vonk_agent_protocol.state_machines import RouteState
from vonk_agent_protocol.telemetry import (
    HostResourceSample,
    TelemetryReadings,
    TelemetryRequest,
    TelemetrySample,
)
from vonk_agent_protocol.wire_model import strict_json_datetime

from .categorized_errors import InvalidValue
from .dialect_insert import dialect_insert
from .fleet_event_contract import NodeTelemetryPayload
from .fleet_events import FleetEventDraft, FleetEventRepository
from .lifecycle.evidence import Residue
from .models import (
    AgentNode,
    NodeEngineSample,
    NodeInventoryResourceSnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
    RecipeRun,
)
from .observed_run_memory import record_observed_run_memory
from .strict_json import warn_unreadable_once
from .warn_once import warn_once


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize SQL timestamps; SQLite drops timezone metadata on round-trip."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TelemetrySampleView(TelemetryReadings):
    """Controller identity and receive time around the host readings of the
    sample contract. Engine readings are not part of it: they have their own
    table and their own reader (``TelemetryRepository.live_engine_readings``)."""

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
        raise InvalidValue(
            "telemetry observation time is outside the accepted window",
            reason=InvalidRequestReason.OUT_OF_RANGE,
        )
    return validated


_SAMPLE_FIELDS = tuple(
    name
    for name in TelemetryReadings.model_fields
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


def _readings(value: dict[str, object]) -> TelemetryReadings:
    return TelemetryReadings.model_validate_json(
        canonical_message({k: v for k, v in value.items() if k in _READING_FIELDS})
    )


_READING_FIELDS = frozenset(TelemetryReadings.model_fields)


def _same_sample(row: NodeTelemetrySample, value: TelemetrySample) -> bool:
    """Whether a stored sample is this one. A replay is compared on the host
    readings: its engine rows were written with it, in the same transaction."""
    return _readings(_view(row).model_dump(mode="json")) == _readings(
        value.model_dump(mode="json")
    )


def _read_view(row: NodeTelemetrySample) -> TelemetrySampleView | None:
    try:
        return _view(row)
    except (AttributeError, TypeError, ValueError):
        warn_unreadable_once("telemetry sample", row.id)
        return None


def _advance_latest(
    session: Session, node_id: str, sample_id: str, observed_at: datetime
) -> bool:
    """Point the node at this sample unless a newer one is already latest.

    Returns whether the pointer moved; the caller owns the Fleet event.
    """
    upsert = dialect_insert(session, NodeTelemetryLatest).values(
        node_id=node_id, sample_id=sample_id
    )
    current = (
        select(NodeTelemetrySample.observed_at)
        # The stored row of the conflict; SQLAlchemy cannot correlate a
        # subquery to the INSERT it sits in, so it names the table itself.
        .where(
            NodeTelemetrySample.id
            == literal_column(f"{NodeTelemetryLatest.__tablename__}.sample_id")
        )
        .scalar_subquery()
    )
    return (
        session.execute(
            upsert.on_conflict_do_update(
                index_elements=[NodeTelemetryLatest.node_id],
                set_={"sample_id": upsert.excluded.sample_id},
                where=current < observed_at,
            ).returning(NodeTelemetryLatest.node_id)
        ).scalar()
        is not None
    )


#: How far back an engine reading still stands for its run. Several samples fit
#: in it, so one slow scrape does not gap the exported series.
ENGINE_LIVE_WINDOW = timedelta(seconds=30)


#: A sample is accepted only within this long of its observation time.
ENGINE_SCAN_BOUND = timedelta(minutes=5)


@dataclass(frozen=True)
class LiveEngineReading:
    node_id: str
    #: The run's published route name.
    alias: str
    #: The stored row; its columns are the wire ``EngineReading`` fields.
    reading: NodeEngineSample


class TelemetryRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._events = FleetEventRepository(sessions, clock=clock)

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
            raise InvalidValue(
                "telemetry sample is duplicated", reason=InvalidRequestReason.DUPLICATE
            )
        last_observed: datetime | None = None
        for value in canonical:
            if last_observed is not None and value.observed_at <= last_observed:
                raise InvalidValue(
                    "telemetry observation times must increase",
                    reason=InvalidRequestReason.OUT_OF_RANGE,
                )
            last_observed = value.observed_at

        stored: list[TelemetrySampleView] = []
        with self._sessions.begin() as session:
            # Agent traffic never locks the node row: admission and publication
            # own it. A sample is its own idempotent write (unique per node,
            # boot and time) and the latest pointer only moves forward, so
            # concurrent batches converge without a parent lock.
            if (
                session.scalar(
                    select(AgentNode.node_id).where(AgentNode.node_id == node_id)
                )
                is None
            ):
                raise ValueError("telemetry node ID is unknown")
            boot_heads: dict[str, datetime | None] = {}
            for value in canonical:
                boot_id = str(value.boot_id)
                if value.boot_id not in boot_heads:
                    boot_heads[value.boot_id] = session.scalar(
                        select(func.max(NodeTelemetrySample.observed_at)).where(
                            NodeTelemetrySample.node_id == node_id,
                            NodeTelemetrySample.boot_id == boot_id,
                        )
                    )
                boot_head = boot_heads[value.boot_id]
                sample_id = str(uuid.uuid4())
                inserted = session.execute(
                    dialect_insert(session, NodeTelemetrySample)
                    .values(
                        id=sample_id,
                        node_id=node_id,
                        received_at=now,
                        **{name: getattr(value, name) for name in _SAMPLE_FIELDS},
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            NodeTelemetrySample.node_id,
                            NodeTelemetrySample.boot_id,
                            NodeTelemetrySample.observed_at,
                        ]
                    )
                    .returning(NodeTelemetrySample.id)
                ).scalar()
                if inserted is None:
                    existing = session.scalars(
                        select(NodeTelemetrySample).where(
                            NodeTelemetrySample.node_id == node_id,
                            NodeTelemetrySample.boot_id == boot_id,
                            NodeTelemetrySample.observed_at == value.observed_at,
                        )
                    ).one()
                    if not _same_sample(existing, value):
                        raise InvalidValue(
                            "telemetry conflicts with stored sample",
                            reason=InvalidRequestReason.CONFLICT,
                        )
                    stored.append(_view(existing))
                    continue
                if boot_head is not None and value.observed_at <= _stored_utc(
                    boot_head
                ):
                    raise InvalidValue(
                        "telemetry sample regresses stored observation time",
                        reason=InvalidRequestReason.OUT_OF_RANGE,
                    )
                resources = HostResourceSample.model_validate_json(
                    canonical_message(
                        value.model_dump(
                            mode="json", include=set(HostResourceSample.model_fields)
                        )
                    )
                )
                session.add(
                    NodeInventoryResourceSnapshot(
                        node_id=node_id,
                        observed_at=value.observed_at,
                        received_at=now,
                        source_sample_id=sample_id,
                        host_resources=resources,
                        evidence_digest=hashlib.sha256(
                            canonical_message(
                                {
                                    "node_id": node_id,
                                    "source_sample_id": sample_id,
                                    "host_resources": resources,
                                }
                            )
                        ).hexdigest(),
                    )
                )
                session.flush()
                session.add_all(
                    NodeEngineSample(
                        sample_id=sample_id, **reading.model_dump(mode="json")
                    )
                    for reading in value.engines or ()
                )
                if _advance_latest(session, node_id, sample_id, value.observed_at):
                    self._events.append_in_session(
                        session,
                        FleetEventDraft(
                            event_type="node-telemetry",
                            node_id=node_id,
                            entity_kind="node-telemetry-latest",
                            entity_id=node_id,
                            payload=NodeTelemetryPayload(
                                node_id=node_id, sample_id=sample_id
                            ),
                        ),
                    )
                # The view is built from what was just written, not re-read.
                stored.append(
                    TelemetrySampleView.model_validate_json(
                        canonical_message(
                            {
                                **value.model_dump(mode="json", exclude={"engines"}),
                                "id": sample_id,
                                "node_id": node_id,
                                "received_at": now,
                            }
                        )
                    )
                )
                boot_heads[value.boot_id] = value.observed_at
            # Derived bookkeeping never refuses valid samples: a failure rolls
            # back only the attribution (2026-11-05: a CHECK failure here
            # refused every upload for 4 h).
            try:
                with session.begin_nested():
                    record_observed_run_memory(
                        session,
                        node_id,
                        [
                            (value.observed_at, value.memory_available_bytes)
                            for value in canonical
                        ],
                        self._events,
                    )
            except Exception as error:  # noqa: BLE001 - derived write, see above
                warn_once(
                    f"attribution-skipped:{node_id}:{type(error).__name__}",
                    "run memory attribution skipped on node %s (%s): samples kept",
                    node_id,
                    type(error).__name__,
                    exc_info=error,
                )
        return tuple(stored)

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

    def live_engine_readings(self, now: datetime) -> list[LiveEngineReading]:
        """The newest reading of each (node, run) received within ``ENGINE_LIVE_WINDOW``.

        The window is by receive time, the Controller's clock, so a Spark whose
        clock runs behind still counts; ``observed_at`` only orders readings and
        bounds the scan (a sample is accepted within 5 minutes of its
        observation, so the bound loses nothing).

        Not only the latest sample: an engine that answered slowly is missing
        from it, and its series would gap exactly when it is busy. A run whose
        route is not published has no alias, so it has no reading here.
        """

        since = _aware_utc(now, label="engine reading clock") - ENGINE_LIVE_WINDOW
        with self._sessions() as session:
            rows = session.execute(
                select(NodeTelemetrySample.node_id, RecipeRun.alias, NodeEngineSample)
                .join(
                    NodeEngineSample,
                    NodeEngineSample.sample_id == NodeTelemetrySample.id,
                )
                .join(RecipeRun, RecipeRun.id == NodeEngineSample.run_id)
                .where(
                    NodeTelemetrySample.received_at >= since,
                    NodeTelemetrySample.observed_at >= since - ENGINE_SCAN_BOUND,
                    RecipeRun.route_state == RouteState.PUBLISHED.value,
                )
                .order_by(NodeTelemetrySample.observed_at)
            ).all()
        newest = {
            (node_id, row.run_id): LiveEngineReading(node_id, alias, row)
            for node_id, alias, row in rows
        }
        return list(newest.values())

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
