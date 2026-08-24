"""Durable, transactionally ordered Fleet event recording and replay."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import event, func, insert, select, true, update
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentOperation,
    FleetEventCursor,
    FleetStreamEvent,
    InstallationNode,
    Job,
    NodeTelemetryLatest,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)

EVENT_TYPES = frozenset(
    {"node-telemetry", "node-profile", "recipe-state", "operation-state"}
)
MAX_EVENT_PAYLOAD_BYTES = 8 * 1024
MAX_REPLAY_BATCH = 128
REPLAY_RETENTION = timedelta(hours=24)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "actor",
        "credential",
        "credentials",
        "endpoint",
        "error",
        "error_detail",
        "evidence",
        "evidence_digest",
        "password",
        "payload",
        "plan",
        "repository",
        "repository_content",
        "request",
        "request_body",
        "result",
        "secret",
        "secret_token",
        "status_reason",
        "token",
    }
)
_PENDING_KEY = "vonk.fleet_event_drafts"
_RECORDER_ATTRIBUTE = "_vonk_fleet_event_recorder"


@dataclass(frozen=True, slots=True)
class FleetEventDraft:
    event_type: str
    node_id: str | None
    entity_kind: str
    entity_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FleetEvent:
    id: int
    event_type: str
    node_id: str | None
    entity_kind: str
    entity_id: str
    payload: dict[str, object]
    occurred_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class FleetRetentionWindow:
    high_watermark: int
    first_retained_id: int | None


@dataclass(frozen=True, slots=True)
class FleetReplayBatch:
    high_watermark: int
    first_retained_id: int | None
    events: tuple[FleetEvent, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _walk_payload(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError("Fleet event payload keys must be strings")
            lowered = key.lower()
            if lowered in _FORBIDDEN_PAYLOAD_KEYS or any(
                marker in lowered
                for marker in (
                    "actor",
                    "credential",
                    "endpoint",
                    "error",
                    "evidence",
                    "password",
                    "repository",
                    "request",
                    "secret",
                    "token",
                )
            ):
                raise ValueError(f"Fleet event payload field {key!r} is forbidden")
            _walk_payload(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _walk_payload(nested)


def _validated_payload(draft: FleetEventDraft) -> dict[str, object]:
    if draft.event_type not in EVENT_TYPES:
        raise ValueError("Fleet event type is not in the public vocabulary")
    if draft.node_id is not None and not 1 <= len(draft.node_id) <= 36:
        raise ValueError("Fleet event node_id must be at most 36 characters")
    if not 1 <= len(draft.entity_kind) <= 32:
        raise ValueError("Fleet event entity_kind must be at most 32 characters")
    if not 1 <= len(draft.entity_id) <= 128:
        raise ValueError("Fleet event entity_id must be at most 128 characters")
    if not isinstance(draft.payload, Mapping):
        raise TypeError("Fleet event payload must be an object")
    _walk_payload(draft.payload)
    payload = dict(draft.payload)
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Fleet event payload must be finite JSON") from error
    if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("Fleet event payload exceeds 8192 bytes")
    return payload


def _as_value(row: Any) -> FleetEvent:
    return FleetEvent(
        id=row.id,
        event_type=row.event_type,
        node_id=row.node_id,
        entity_kind=row.entity_kind,
        entity_id=row.entity_id,
        payload=dict(row.payload),
        occurred_at=_database_utc(row.occurred_at),
        expires_at=_database_utc(row.expires_at),
    )


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _cursor_lock_statement() -> Any:
    return (
        select(FleetEventCursor.last_id)
        .where(FleetEventCursor.singleton_id == 1)
        .with_for_update()
    )


def _sqlite_cursor_allocation_statement() -> Any:
    return (
        update(FleetEventCursor)
        .where(FleetEventCursor.singleton_id == 1)
        .values(last_id=FleetEventCursor.last_id + 1)
        .returning(FleetEventCursor.last_id)
    )


class FleetEventRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def append_in_session(
        self, session: Session, draft: FleetEventDraft
    ) -> FleetEvent:
        payload = _validated_payload(draft)
        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("Fleet event clock must return a timezone-aware value")
        occurred_at = occurred_at.astimezone(UTC)
        expires_at = occurred_at + REPLAY_RETENTION
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            event_id = connection.execute(
                _sqlite_cursor_allocation_statement()
            ).scalar_one_or_none()
            if event_id is None:
                raise RuntimeError("fleet event cursor singleton is not initialized")
        else:
            last_id = connection.execute(
                _cursor_lock_statement()
            ).scalar_one_or_none()
            if last_id is None:
                raise RuntimeError("fleet event cursor singleton is not initialized")
            event_id = last_id + 1
            connection.execute(
                update(FleetEventCursor)
                .where(FleetEventCursor.singleton_id == 1)
                .values(last_id=event_id)
            )
        values = {
            "id": event_id,
            "event_type": draft.event_type,
            "node_id": draft.node_id,
            "entity_kind": draft.entity_kind,
            "entity_id": draft.entity_id,
            "payload": payload,
            "occurred_at": occurred_at,
            "expires_at": expires_at,
        }
        connection.execute(insert(FleetStreamEvent).values(**values))
        return FleetEvent(**values)

    def high_watermark(self) -> int:
        with self._sessions() as session:
            value = session.scalar(
                select(FleetEventCursor.last_id).where(
                    FleetEventCursor.singleton_id == 1
                )
            )
        if value is None:
            raise RuntimeError("fleet event cursor singleton is not initialized")
        return value

    def retention_window(self, now: datetime) -> FleetRetentionWindow:
        with self._sessions() as session:
            row = session.execute(
                select(
                    FleetEventCursor.last_id,
                    select(func.min(FleetStreamEvent.id))
                    .where(FleetStreamEvent.expires_at > now)
                    .scalar_subquery(),
                ).where(
                    FleetEventCursor.singleton_id == 1
                )
            ).one_or_none()
        if row is None:
            raise RuntimeError("fleet event cursor singleton is not initialized")
        return FleetRetentionWindow(
            high_watermark=row[0],
            first_retained_id=row[1],
        )

    def after(
        self, last_id: int, now: datetime, *, limit: int
    ) -> tuple[FleetEvent, ...]:
        if not 1 <= limit <= MAX_REPLAY_BATCH:
            raise ValueError("Fleet event read limit must be between 1 and 128")
        with self._sessions() as session:
            rows = session.scalars(
                select(FleetStreamEvent)
                .where(
                    FleetStreamEvent.id > last_id,
                    FleetStreamEvent.expires_at > now,
                )
                .order_by(FleetStreamEvent.id)
                .limit(limit)
            ).all()
            return tuple(_as_value(row) for row in rows)

    def replay_after(
        self, last_id: int, now: datetime, *, limit: int
    ) -> FleetReplayBatch:
        """Read replay rows and continuity metadata in one database snapshot."""

        if type(last_id) is not int or not 0 <= last_id <= 9_223_372_036_854_775_807:
            raise ValueError("Fleet event cursor is invalid")
        if not 1 <= limit <= MAX_REPLAY_BATCH:
            raise ValueError("Fleet event read limit must be between 1 and 128")
        replay = (
            select(
                FleetStreamEvent.id,
                FleetStreamEvent.event_type,
                FleetStreamEvent.node_id,
                FleetStreamEvent.entity_kind,
                FleetStreamEvent.entity_id,
                FleetStreamEvent.payload,
                FleetStreamEvent.occurred_at,
                FleetStreamEvent.expires_at,
            )
            .where(
                FleetStreamEvent.id > last_id,
                FleetStreamEvent.expires_at > now,
            )
            .order_by(FleetStreamEvent.id)
            .limit(limit)
            .subquery()
        )
        first_retained = (
            select(func.min(FleetStreamEvent.id))
            .where(FleetStreamEvent.expires_at > now)
            .scalar_subquery()
        )
        statement = (
            select(
                FleetEventCursor.last_id,
                first_retained,
                replay.c.id,
                replay.c.event_type,
                replay.c.node_id,
                replay.c.entity_kind,
                replay.c.entity_id,
                replay.c.payload,
                replay.c.occurred_at,
                replay.c.expires_at,
            )
            .select_from(FleetEventCursor)
            .outerjoin(replay, true())
            .where(FleetEventCursor.singleton_id == 1)
            .order_by(replay.c.id)
        )
        with self._sessions() as session:
            rows = session.execute(statement).all()
        if not rows:
            raise RuntimeError("fleet event cursor singleton is not initialized")
        events = tuple(
            FleetEvent(
                id=row[2],
                event_type=row[3],
                node_id=row[4],
                entity_kind=row[5],
                entity_id=row[6],
                payload=dict(row[7]),
                occurred_at=_database_utc(row[8]),
                expires_at=_database_utc(row[9]),
            )
            for row in rows
            if row[2] is not None
        )
        return FleetReplayBatch(
            high_watermark=rows[0][0],
            first_retained_id=rows[0][1],
            events=events,
        )


class FleetEventRecorder:
    """Observe public Fleet state transitions on one session factory."""

    _tracked_fields: ClassVar[dict[type[object], tuple[str, ...]]] = {
        NodeTelemetryLatest: ("sample_id",),
        RecipeInstallation: ("state",),
        InstallationNode: ("state", "installed_bytes"),
        RecipeRun: ("state", "route_state"),
        RunNode: ("state", "observed_memory_bytes"),
        Job: ("state",),
        AgentOperation: ("state", "current_attempt"),
    }

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._repository = FleetEventRepository(sessions, clock=clock)
        self._listeners = (
            ("before_flush", self._before_flush),
            ("after_flush", self._after_flush),
            ("after_rollback", self._after_rollback),
            ("after_soft_rollback", self._after_soft_rollback),
            ("after_transaction_end", self._after_transaction_end),
        )

    @classmethod
    def install(
        cls,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> FleetEventRecorder:
        installed = getattr(sessions, _RECORDER_ATTRIBUTE, None)
        if installed is not None:
            return installed
        recorder = cls(sessions, clock=clock)
        for name, listener in recorder._listeners:
            event.listen(sessions, name, listener)
        setattr(sessions, _RECORDER_ATTRIBUTE, recorder)
        return recorder

    def uninstall(self) -> None:
        if getattr(self._sessions, _RECORDER_ATTRIBUTE, None) is not self:
            return
        for name, listener in self._listeners:
            event.remove(self._sessions, name, listener)
        delattr(self._sessions, _RECORDER_ATTRIBUTE)

    def _before_flush(
        self, session: Session, _flush_context: object, _instances: object
    ) -> None:
        candidates: list[object] = []
        for value in session.new:
            if type(value) in self._tracked_fields:
                candidates.append(value)
        for value in session.dirty:
            fields = self._tracked_fields.get(type(value))
            if fields is None:
                continue
            state = sqlalchemy_inspect(value)
            if any(state.attrs[field].history.has_changes() for field in fields):
                candidates.append(value)
        if candidates:
            session.info[_PENDING_KEY] = candidates
        else:
            session.info.pop(_PENDING_KEY, None)

    def _after_flush(self, session: Session, _flush_context: object) -> None:
        candidates = session.info.pop(_PENDING_KEY, ())
        for value in sorted(candidates, key=self._candidate_order):
            self._repository.append_in_session(session, self._render(value))

    @classmethod
    def _candidate_order(cls, value: object) -> tuple[int, str]:
        source_order = tuple(cls._tracked_fields)
        identifier = getattr(value, "id", None) or getattr(value, "node_id", "")
        return source_order.index(type(value)), str(identifier)

    @staticmethod
    def _after_rollback(session: Session) -> None:
        session.info.pop(_PENDING_KEY, None)

    @staticmethod
    def _after_soft_rollback(session: Session, _previous: object) -> None:
        session.info.pop(_PENDING_KEY, None)

    @staticmethod
    def _after_transaction_end(session: Session, transaction: object) -> None:
        if getattr(transaction, "parent", None) is None:
            session.info.pop(_PENDING_KEY, None)

    @staticmethod
    def _render(value: object) -> FleetEventDraft:
        if isinstance(value, NodeTelemetryLatest):
            return FleetEventDraft(
                event_type="node-telemetry",
                node_id=value.node_id,
                entity_kind="node-telemetry-latest",
                entity_id=value.node_id,
                payload={
                    "schema_version": 1,
                    "node_id": value.node_id,
                    "sample_id": value.sample_id,
                },
            )
        if isinstance(value, RecipeInstallation):
            entity_kind = "recipe-installation"
            payload = {
                "schema_version": 1,
                "entity_kind": entity_kind,
                "entity_id": value.id,
                "recipe_revision_id": value.recipe_revision_id,
                "mapping_id": value.mapping_id,
                "mapping_generation": value.mapping_generation,
                "state": value.state,
            }
            return FleetEventDraft(
                "recipe-state", None, entity_kind, value.id, payload
            )
        if isinstance(value, InstallationNode):
            entity_kind = "installation-node"
            payload = {
                "schema_version": 1,
                "entity_kind": entity_kind,
                "entity_id": value.id,
                "installation_id": value.installation_id,
                "node_id": value.node_id,
                "rank": value.rank,
                "role": value.role,
                "state": value.state,
                "installed_bytes": value.installed_bytes,
                "required_bytes": value.required_bytes,
            }
            return FleetEventDraft(
                "recipe-state", value.node_id, entity_kind, value.id, payload
            )
        if isinstance(value, RecipeRun):
            entity_kind = "recipe-run"
            payload = {
                "schema_version": 1,
                "entity_kind": entity_kind,
                "entity_id": value.id,
                "installation_id": value.installation_id,
                "mapping_id": value.mapping_id,
                "mapping_generation": value.mapping_generation,
                "alias": value.alias,
                "state": value.state,
                "route_state": value.route_state,
            }
            return FleetEventDraft(
                "recipe-state", None, entity_kind, value.id, payload
            )
        if isinstance(value, RunNode):
            entity_kind = "run-node"
            payload = {
                "schema_version": 1,
                "entity_kind": entity_kind,
                "entity_id": value.id,
                "run_id": value.run_id,
                "node_id": value.node_id,
                "rank": value.rank,
                "role": value.role,
                "state": value.state,
                "reserved_memory_bytes": value.reserved_memory_bytes,
                "observed_memory_bytes": value.observed_memory_bytes,
            }
            return FleetEventDraft(
                "recipe-state", value.node_id, entity_kind, value.id, payload
            )
        if isinstance(value, Job):
            entity_kind = "job"
            payload = {
                "schema_version": 1,
                "entity_kind": entity_kind,
                "entity_id": value.id,
                "kind": value.kind,
                "state": value.state,
                "target_count": len(value.targets),
            }
            return FleetEventDraft(
                "operation-state", None, entity_kind, value.id, payload
            )
        if isinstance(value, AgentOperation):
            entity_kind = "agent-operation"
            payload = {
                "schema_version": 1,
                "entity_kind": entity_kind,
                "entity_id": value.id,
                "parent_job_id": value.parent_job_id,
                "node_id": value.node_id,
                "kind": value.kind,
                "state": value.state,
                "attempt": value.current_attempt,
            }
            return FleetEventDraft(
                "operation-state", value.node_id, entity_kind, value.id, payload
            )
        raise TypeError(f"Unsupported Fleet event source: {type(value).__name__}")
