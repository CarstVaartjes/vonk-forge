"""Bounded retention for raw telemetry, inventory history and Fleet events."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .admission_locking import is_admission_contention, label_transaction
from .fleet_event_contract import NodeTelemetryPayload
from .fleet_events import FleetEventDraft, FleetEventRepository
from .models import (
    AgentNode,
    FleetStreamEvent,
    NodeInventorySnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
)

_MAX_MAINTENANCE_LIMIT = 25_000
#: One pass deletes at most this many rows per table.  Maintenance holds rows
#: that admission locks (agent nodes, inventory snapshots) for as long as its
#: transaction lives, so a pass is small and frequent instead of one large batch
#: that kept admission refused for seconds out of every fifteen.
_DEFAULT_MAINTENANCE_LIMIT = 500
#: Maintenance never queues for a row an admission holds: a queued writer refuses
#: every NOWAIT admission behind it.
_MAINTENANCE_LOCK_TIMEOUT_MS = 250
_SQL_KEY_CHUNK = 250
_MAINTENANCE_INTERVAL = timedelta(seconds=15)
#: Raw samples serve the live Fleet view only; history lives in Prometheus.
_RAW_RETENTION = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _validated_limit(value: int, *, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_MAINTENANCE_LIMIT:
        raise ValueError(f"{label} must be between 1 and 25000")
    return value


def _polite(session: Session, name: str) -> None:
    """Label this maintenance transaction and bound every lock wait it makes."""

    if session.connection().dialect.name != "postgresql":
        return
    label_transaction(session, name)
    session.execute(
        text("SELECT set_config('lock_timeout', :t, true)"),
        {"t": f"{_MAINTENANCE_LOCK_TIMEOUT_MS}ms"},
    )


def _lock_nodes(session: Session, node_ids: list[str]) -> set[str]:
    """Lock the nodes whose samples are pruned; return those that were free.

    A node an admission (or anything else) holds is skipped, never waited for:
    its samples are simply pruned on a later pass.
    """

    ordered = sorted(set(node_ids))
    if session.connection().dialect.name == "sqlite":
        for node_id in ordered:
            session.execute(
                update(AgentNode)
                .where(AgentNode.node_id == node_id)
                .values(node_id=AgentNode.node_id)
            )
        return set(ordered)
    locked: set[str] = set()
    for chunk in TelemetryMaintenance._chunks(ordered):
        locked.update(
            session.scalars(
                select(AgentNode.node_id)
                .where(AgentNode.node_id.in_(chunk))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode, skip_locked=True)
            ).all()
        )
    return locked


def _raw_candidate_statement(
    *,
    cutoff: datetime,
    limit: int,
    sample_ids: list[str] | None = None,
):
    statement = select(
        NodeTelemetrySample.id,
        NodeTelemetrySample.node_id,
        NodeTelemetrySample.observed_at,
    ).where(NodeTelemetrySample.observed_at < cutoff)
    if sample_ids is not None:
        statement = statement.where(NodeTelemetrySample.id.in_(sample_ids))
    return statement.order_by(
        NodeTelemetrySample.observed_at,
        NodeTelemetrySample.node_id,
        NodeTelemetrySample.id,
    ).limit(limit)


class TelemetryMaintenance:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def run_once(self, delete_limit: int = _DEFAULT_MAINTENANCE_LIMIT) -> None:
        limit = _validated_limit(delete_limit, label="delete limit")
        now = _aware_utc(self._clock(), label="telemetry maintenance clock")
        events = FleetEventRepository(self._sessions, clock=lambda: now)
        cutoff = now - _RAW_RETENTION
        with self._sessions() as session:
            raw_candidates = session.execute(
                _raw_candidate_statement(cutoff=cutoff, limit=limit)
            ).all()
        for stage in (
            lambda session: self._prune_raw(
                session,
                cutoff=cutoff,
                limit=limit,
                events=events,
                candidates=raw_candidates,
            ),
            lambda session: self._prune_events(session, now=now, limit=limit),
            lambda session: self._prune_inventory(session, cutoff=cutoff, limit=limit),
        ):
            try:
                with self._sessions.begin() as session:
                    _polite(session, "telemetry-maintenance")
                    stage(session)
            except OperationalError as error:
                if not is_admission_contention(error):
                    raise
                # Something else owns the rows; retention is eventually
                # consistent, so this stage runs again on the next pass.

    @staticmethod
    def _prune_inventory(session: Session, *, cutoff: datetime, limit: int) -> None:
        """Keep a day of inventory history and always each node's newest row.

        Admission reads only recent inventory; unbounded history only slowed
        every read that looks for the newest row.
        """

        newest = (
            select(
                NodeInventorySnapshot.node_id.label("node_id"),
                func.max(NodeInventorySnapshot.observed_at).label("observed_at"),
            )
            .group_by(NodeInventorySnapshot.node_id)
            .subquery()
        )
        stale = list(
            session.scalars(
                select(NodeInventorySnapshot.id)
                .join(newest, NodeInventorySnapshot.node_id == newest.c.node_id)
                .where(
                    NodeInventorySnapshot.observed_at < cutoff,
                    NodeInventorySnapshot.observed_at < newest.c.observed_at,
                )
                .order_by(NodeInventorySnapshot.observed_at, NodeInventorySnapshot.id)
                .limit(limit)
                .with_for_update(of=NodeInventorySnapshot, skip_locked=True)
            )
        )
        for chunk in TelemetryMaintenance._chunks(stale):
            session.execute(
                delete(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.id.in_(chunk))
                .execution_options(synchronize_session=False)
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
        candidates: Sequence[Row[str, str, datetime]],
    ) -> None:
        free = _lock_nodes(
            session,
            [node_id for _sample_id, node_id, _observed_at in candidates],
        )
        candidates = [row for row in candidates if row[1] in free]
        sample_ids = [sample_id for sample_id, _node_id, _observed_at in candidates]

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
                    payload=NodeTelemetryPayload(
                        node_id=pointer.node_id, sample_id=pointer.sample_id
                    ),
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
