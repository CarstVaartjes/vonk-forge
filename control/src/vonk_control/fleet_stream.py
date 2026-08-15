"""Bounded resumable Server-Sent Events delivery for Fleet state."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime

from .fleet_events import FleetEvent, FleetEventRepository, FleetRetentionWindow
from .fleet_projection import FleetProjection, FleetSnapshot, telemetry_point
from .telemetry import TelemetryRepository, TelemetrySampleView

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
POLL_INTERVAL_SECONDS = 1.0
KEEPALIVE_INTERVAL_SECONDS = 15.0
RETRY_MILLISECONDS = 2_000


def parse_last_event_id(values: Sequence[str]) -> int | None:
    """Parse Starlette ``headers.getlist()`` output without HTTP coercion."""

    if not values:
        return None
    if len(values) != 1:
        raise ValueError("Last-Event-ID must occur exactly once")
    value = values[0]
    if not value or any(character < "0" or character > "9" for character in value):
        raise ValueError("Last-Event-ID must be unsigned ASCII decimal")
    parsed = int(value)
    if parsed > MAX_SIGNED_BIGINT:
        raise ValueError("Last-Event-ID exceeds signed BIGINT")
    return parsed


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Fleet stream timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _event_frame(
    identifier: int,
    event_type: str,
    data: Mapping[str, object],
    *,
    retry: bool,
) -> str:
    lines = []
    if retry:
        lines.append(f"retry: {RETRY_MILLISECONDS}")
    lines.extend((f"id: {identifier}", f"event: {event_type}", f"data: {_json(data)}"))
    return "\n".join(lines) + "\n\n"


def _keepalive_frame(now: datetime, *, retry: bool) -> str:
    lines = []
    if retry:
        lines.append(f"retry: {RETRY_MILLISECONDS}")
    lines.append(f": keepalive {_iso(now)}")
    return "\n".join(lines) + "\n\n"


def _snapshot_data(snapshot: FleetSnapshot, reason: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "reset_reason": reason,
        "snapshot": snapshot.model_dump(mode="json"),
    }


class FleetStream:
    """Replay the durable outbox without holding resources across suspension."""

    def __init__(
        self,
        events: FleetEventRepository,
        telemetry: TelemetryRepository,
        projection: FleetProjection,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._events = events
        self._telemetry = telemetry
        self._projection = projection
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep

    async def events(self, last_event_id: int | None) -> AsyncIterator[str]:
        retry = True
        current_cursor: int
        try:
            if last_event_id is None:
                current_cursor = self._events.high_watermark()
                snapshot = self._projection.read_at(current_cursor)
                yield _event_frame(
                    current_cursor,
                    "fleet-snapshot",
                    _snapshot_data(snapshot, "initial"),
                    retry=retry,
                )
                retry = False
            else:
                window = self._events.retention_window(self._clock())
                reset_reason = self._reset_reason(last_event_id, window)
                if reset_reason is not None:
                    current_cursor = window.high_watermark
                    snapshot = self._projection.read_at(current_cursor)
                    yield _event_frame(
                        current_cursor,
                        "fleet-snapshot",
                        _snapshot_data(snapshot, reset_reason),
                        retry=retry,
                    )
                    retry = False
                else:
                    current_cursor = last_event_id

            last_emit = self._monotonic()
            last_poll: float | None = None
            while True:
                if last_poll is not None:
                    elapsed = self._monotonic() - last_poll
                    if elapsed < POLL_INTERVAL_SECONDS:
                        await self._sleep(POLL_INTERVAL_SECONDS - elapsed)
                now = self._clock()
                last_poll = self._monotonic()
                batch = self._events.after(current_cursor, now, limit=128)
                self._validate_order(batch, current_cursor)
                if batch:
                    samples = self._hydrate_telemetry(batch)
                    if samples is None:
                        current_cursor = self._events.high_watermark()
                        snapshot = self._projection.read_at(current_cursor)
                        yield _event_frame(
                            current_cursor,
                            "fleet-snapshot",
                            _snapshot_data(snapshot, "missing-telemetry-sample"),
                            retry=retry,
                        )
                        retry = False
                        last_emit = self._monotonic()
                        continue
                    for event in batch:
                        data = self._event_data(event, samples)
                        yield _event_frame(
                            event.id,
                            event.event_type,
                            data,
                            retry=retry,
                        )
                        current_cursor = event.id
                        retry = False
                        last_emit = self._monotonic()
                    continue
                if self._monotonic() - last_emit >= KEEPALIVE_INTERVAL_SECONDS:
                    yield _keepalive_frame(self._clock(), retry=retry)
                    retry = False
                    last_emit = self._monotonic()
        finally:
            # Reads own and close their sessions before every await/yield. The
            # stream starts no producer task and owns no queue to clean up.
            pass

    @staticmethod
    def _reset_reason(
        last_event_id: int, window: FleetRetentionWindow
    ) -> str | None:
        if last_event_id > window.high_watermark:
            return "cursor-ahead"
        if window.first_retained_id is None:
            return "retention-gap" if last_event_id < window.high_watermark else None
        if last_event_id < window.first_retained_id - 1:
            return "retention-gap"
        return None

    @staticmethod
    def _validate_order(batch: Sequence[FleetEvent], cursor: int) -> None:
        previous = cursor
        for event in batch:
            if event.id <= previous:
                raise RuntimeError("Fleet stream events are not strictly ordered")
            previous = event.id

    def _hydrate_telemetry(
        self, batch: Sequence[FleetEvent]
    ) -> dict[str, TelemetrySampleView] | None:
        sample_ids: list[str] = []
        for event in batch:
            if event.event_type != "node-telemetry":
                continue
            sample_id = event.payload.get("sample_id")
            if not isinstance(sample_id, str):
                return None
            if sample_id not in sample_ids:
                sample_ids.append(sample_id)
        samples = self._telemetry.by_ids(tuple(sample_ids)) if sample_ids else {}
        if any(sample_id not in samples for sample_id in sample_ids):
            return None
        return samples

    @staticmethod
    def _event_data(
        event: FleetEvent, samples: Mapping[str, TelemetrySampleView]
    ) -> dict[str, object]:
        if event.event_type == "node-telemetry":
            sample_id = event.payload.get("sample_id")
            sample = samples.get(sample_id) if isinstance(sample_id, str) else None
            if sample is None or sample.node_id != event.node_id:
                raise RuntimeError("Fleet telemetry event hydration is inconsistent")
            return {
                "schema_version": 1,
                "node_id": sample.node_id,
                "sample": telemetry_point(sample).model_dump(mode="json"),
            }
        if event.event_type not in {"recipe-state", "operation-state"}:
            raise RuntimeError("Fleet stream event type is invalid")
        return {
            "schema_version": 1,
            "projection_refresh_required": True,
            "change": {
                "entity_kind": event.entity_kind,
                "entity_id": event.entity_id,
                "node_id": event.node_id,
                "occurred_at": _iso(event.occurred_at),
                "fields": dict(event.payload),
            },
        }
