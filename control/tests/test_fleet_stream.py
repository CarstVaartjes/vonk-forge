from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.browser_auth import BrowserAuthService
from vonk_control.fleet_events import FleetEvent, FleetRetentionWindow
from vonk_control.fleet_projection import FleetSnapshot
from vonk_control.fleet_stream import FleetStream, parse_last_event_id
from vonk_control.models import Base, User
from vonk_control.passwords import hash_password
from vonk_control.telemetry import TelemetryDetailsInput, TelemetrySampleView

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
COMMIT = "a" * 40
NODE_ID = "spk_" + "1" * 32
SAMPLE_ID = "00000000-0000-4000-8000-000000000001"
PASSWORD = "correct horse battery staple"


class Projection:
    def __init__(self) -> None:
        self.cursors: list[int] = []

    def read_at(self, cursor: int) -> FleetSnapshot:
        self.cursors.append(cursor)
        return FleetSnapshot(
            event_cursor=cursor,
            generated_at=NOW,
            repository_commit=COMMIT,
            nodes=[],
        )


class Events:
    def __init__(
        self,
        *,
        high_watermark: int,
        first_retained_id: int | None,
        batches: list[tuple[FleetEvent, ...] | BaseException] | None = None,
    ) -> None:
        self.high_watermark_value = high_watermark
        self.first_retained_id = first_retained_id
        self.batches = list(batches or [])
        self.high_watermark_calls = 0
        self.retention_calls: list[datetime] = []
        self.after_calls: list[tuple[int, datetime, int]] = []

    def high_watermark(self) -> int:
        self.high_watermark_calls += 1
        return self.high_watermark_value

    def retention_window(self, now: datetime) -> FleetRetentionWindow:
        self.retention_calls.append(now)
        return FleetRetentionWindow(
            high_watermark=self.high_watermark_value,
            first_retained_id=self.first_retained_id,
        )

    def after(
        self, last_id: int, now: datetime, *, limit: int
    ) -> tuple[FleetEvent, ...]:
        self.after_calls.append((last_id, now, limit))
        if not self.batches:
            return ()
        value = self.batches.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class Telemetry:
    def __init__(self, values: dict[str, TelemetrySampleView] | None = None) -> None:
        self.values = values or {}
        self.calls: list[tuple[str, ...]] = []

    def by_ids(self, sample_ids: tuple[str, ...]) -> dict[str, TelemetrySampleView]:
        self.calls.append(sample_ids)
        return {
            sample_id: self.values[sample_id]
            for sample_id in sample_ids
            if sample_id in self.values
        }


class Timing:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.seconds

    def clock(self) -> datetime:
        return NOW + timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.seconds += seconds


def _event(
    identifier: int,
    event_type: str,
    *,
    payload: dict[str, object],
    node_id: str | None = None,
    entity_kind: str = "entity",
) -> FleetEvent:
    return FleetEvent(
        id=identifier,
        event_type=event_type,
        node_id=node_id,
        entity_kind=entity_kind,
        entity_id=f"entity-{identifier}",
        payload=payload,
        occurred_at=NOW + timedelta(seconds=identifier),
        expires_at=NOW + timedelta(hours=24),
    )


def _sample() -> TelemetrySampleView:
    return TelemetrySampleView(
        id=SAMPLE_ID,
        node_id=NODE_ID,
        boot_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        sequence=3,
        observed_at=NOW - timedelta(seconds=2),
        received_at=NOW - timedelta(seconds=1),
        cpu_utilization_percent=12.5,
        load_average_1m=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        temperature_c=None,
        power_watts=None,
        network_receive_bytes_per_second=None,
        network_transmit_bytes_per_second=None,
        gap_samples=0,
        details=TelemetryDetailsInput(
            accelerator_name="NVIDIA GB10",
            accelerator_performance_state="P0",
        ),
    )


def _parsed_frame(frame: str) -> tuple[dict[str, str], object | None]:
    fields: dict[str, str] = {}
    data: object | None = None
    for line in frame.rstrip("\n").splitlines():
        if line.startswith(":"):
            fields["comment"] = line.removeprefix(": ")
            continue
        name, value = line.split(": ", 1)
        if name == "data":
            data = json.loads(value)
        else:
            fields[name] = value
    return fields, data


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        (["0"], 0),
        (["9223372036854775807"], 9_223_372_036_854_775_807),
    ],
)
def test_last_event_id_accepts_only_one_unsigned_ascii_bigint(
    values: list[str], expected: int | None
) -> None:
    assert parse_last_event_id(values) == expected


@pytest.mark.parametrize(
    "values",
    [
        [""],
        ["1", "2"],
        ["+1"],
        ["-1"],
        [" 1"],
        ["1 "],
        ["١"],
        ["1,2"],
        ["9223372036854775808"],
    ],
)
def test_last_event_id_rejects_duplicates_signs_spacing_unicode_and_overflow(
    values: list[str],
) -> None:
    with pytest.raises(ValueError, match="Last-Event-ID"):
        parse_last_event_id(values)


def test_resume_replays_ordered_events_with_one_hydration_and_refresh_semantics() -> None:
    telemetry_event = _event(
        6,
        "node-telemetry",
        node_id=NODE_ID,
        entity_kind="node-telemetry-latest",
        payload={"schema_version": 1, "node_id": NODE_ID, "sample_id": SAMPLE_ID},
    )
    recipe_event = _event(
        7,
        "recipe-state",
        node_id=NODE_ID,
        entity_kind="installation-node",
        payload={
            "schema_version": 1,
            "entity_kind": "installation-node",
            "entity_id": "rank-1",
            "state": "installed",
        },
    )
    events = Events(
        high_watermark=7,
        first_retained_id=1,
        batches=[(telemetry_event, recipe_event)],
    )
    projection = Projection()
    telemetry = Telemetry({SAMPLE_ID: _sample()})
    timing = Timing()
    stream = FleetStream(
        events,
        telemetry,
        projection,
        clock=timing.clock,
        monotonic=timing.monotonic,
        sleep=timing.sleep,
    )

    async def read() -> tuple[str, str]:
        generator = stream.events(5)
        try:
            return await anext(generator), await anext(generator)
        finally:
            await generator.aclose()

    telemetry_frame, recipe_frame = asyncio.run(read())
    telemetry_fields, telemetry_data = _parsed_frame(telemetry_frame)
    recipe_fields, recipe_data = _parsed_frame(recipe_frame)

    assert telemetry_fields == {
        "retry": "2000",
        "id": "6",
        "event": "node-telemetry",
    }
    assert telemetry_data == {
        "node_id": NODE_ID,
        "sample": {
            "boot_id": "00000000-0000-4000-8000-000000000002",
            "cpu_utilization_percent": 12.5,
            "details": {
                "accelerator_name": "NVIDIA GB10",
                "accelerator_performance_state": "P0",
            },
            "disk_free_bytes": None,
            "disk_total_bytes": None,
            "gap_samples": 0,
            "gpu_memory_free_bytes": None,
            "gpu_memory_total_bytes": None,
            "gpu_utilization_percent": None,
            "id": SAMPLE_ID,
            "load_average_1m": None,
            "memory_available_bytes": None,
            "memory_total_bytes": None,
            "network_receive_bytes_per_second": None,
            "network_transmit_bytes_per_second": None,
            "node_id": NODE_ID,
            "observed_at": "2026-08-15T11:59:58Z",
            "power_watts": None,
            "received_at": "2026-08-15T11:59:59Z",
            "sequence": 3,
            "temperature_c": None,
        },
        "schema_version": 1,
    }
    assert recipe_fields == {"id": "7", "event": "recipe-state"}
    assert recipe_data == {
        "change": {
            "entity_id": "entity-7",
            "entity_kind": "installation-node",
            "fields": {
                "entity_id": "rank-1",
                "entity_kind": "installation-node",
                "schema_version": 1,
                "state": "installed",
            },
            "node_id": NODE_ID,
            "occurred_at": "2026-08-15T12:00:07Z",
        },
        "projection_refresh_required": True,
        "schema_version": 1,
    }
    assert events.retention_calls == [NOW]
    assert events.after_calls == [(5, NOW, 128)]
    assert telemetry.calls == [(SAMPLE_ID,)]
    assert projection.cursors == []


def test_initial_snapshot_uses_watermark_then_replays_later_event() -> None:
    operation_event = _event(
        6,
        "operation-state",
        payload={"schema_version": 1, "entity_id": "job-1", "state": "running"},
        entity_kind="job",
    )
    events = Events(
        high_watermark=5,
        first_retained_id=1,
        batches=[(operation_event,)],
    )
    projection = Projection()
    timing = Timing()
    stream = FleetStream(
        events,
        Telemetry(),
        projection,
        clock=timing.clock,
        monotonic=timing.monotonic,
        sleep=timing.sleep,
    )

    async def read() -> tuple[str, str]:
        generator = stream.events(None)
        try:
            return await anext(generator), await anext(generator)
        finally:
            await generator.aclose()

    snapshot_frame, replay_frame = asyncio.run(read())
    snapshot_fields, snapshot_data = _parsed_frame(snapshot_frame)
    replay_fields, replay_data = _parsed_frame(replay_frame)

    assert snapshot_fields == {
        "retry": "2000",
        "id": "5",
        "event": "fleet-snapshot",
    }
    assert snapshot_data == {
        "reset_reason": "initial",
        "schema_version": 1,
        "snapshot": {
            "event_cursor": 5,
            "generated_at": "2026-08-15T12:00:00Z",
            "nodes": [],
            "repository_commit": COMMIT,
            "schema_version": 1,
        },
    }
    assert replay_fields == {"id": "6", "event": "operation-state"}
    assert replay_data["projection_refresh_required"] is True
    assert events.high_watermark_calls == 1
    assert projection.cursors == [5]
    assert events.after_calls == [(5, NOW, 128)]


@pytest.mark.parametrize(
    ("cursor", "high_watermark", "first_retained", "reason"),
    [
        (3, 10, 5, "retention-gap"),
        (9, 10, None, "retention-gap"),
        (11, 10, 5, "cursor-ahead"),
    ],
)
def test_invalid_resume_window_resets_to_current_snapshot(
    cursor: int,
    high_watermark: int,
    first_retained: int | None,
    reason: str,
) -> None:
    events = Events(
        high_watermark=high_watermark,
        first_retained_id=first_retained,
    )
    projection = Projection()
    timing = Timing()
    stream = FleetStream(
        events,
        Telemetry(),
        projection,
        clock=timing.clock,
        monotonic=timing.monotonic,
        sleep=timing.sleep,
    )

    async def read() -> str:
        generator = stream.events(cursor)
        try:
            return await anext(generator)
        finally:
            await generator.aclose()

    frame = asyncio.run(read())
    fields, data = _parsed_frame(frame)

    assert fields == {
        "retry": "2000",
        "id": str(high_watermark),
        "event": "fleet-snapshot",
    }
    assert data["reset_reason"] == reason
    assert data["snapshot"]["event_cursor"] == high_watermark
    assert projection.cursors == [high_watermark]
    assert events.after_calls == []


def test_missing_telemetry_reference_forces_snapshot_reset() -> None:
    events = Events(
        high_watermark=9,
        first_retained_id=1,
        batches=[
            (
                _event(
                    6,
                    "node-telemetry",
                    node_id=NODE_ID,
                    payload={
                        "schema_version": 1,
                        "node_id": NODE_ID,
                        "sample_id": SAMPLE_ID,
                    },
                ),
            )
        ],
    )
    projection = Projection()
    timing = Timing()
    stream = FleetStream(
        events,
        Telemetry(),
        projection,
        clock=timing.clock,
        monotonic=timing.monotonic,
        sleep=timing.sleep,
    )

    async def read() -> str:
        generator = stream.events(5)
        try:
            return await anext(generator)
        finally:
            await generator.aclose()

    fields, data = _parsed_frame(asyncio.run(read()))

    assert fields == {
        "retry": "2000",
        "id": "9",
        "event": "fleet-snapshot",
    }
    assert data["reset_reason"] == "missing-telemetry-sample"
    assert data["snapshot"]["event_cursor"] == 9
    assert projection.cursors == [9]


def test_empty_stream_polls_once_per_second_and_keeps_alive_by_fifteen_seconds() -> None:
    events = Events(high_watermark=0, first_retained_id=None)
    timing = Timing()
    stream = FleetStream(
        events,
        Telemetry(),
        Projection(),
        clock=timing.clock,
        monotonic=timing.monotonic,
        sleep=timing.sleep,
    )

    async def read() -> str:
        generator = stream.events(0)
        try:
            return await anext(generator)
        finally:
            await generator.aclose()

    fields, data = _parsed_frame(asyncio.run(read()))

    assert fields == {
        "retry": "2000",
        "comment": "keepalive 2026-08-15T12:00:15Z",
    }
    assert data is None
    assert timing.sleeps == [1.0] * 15
    assert [call[0] for call in events.after_calls] == [0] * 16
    assert [call[1] for call in events.after_calls] == [
        NOW + timedelta(seconds=second) for second in range(16)
    ]


def test_database_failure_terminates_without_emitting_or_advancing() -> None:
    events = Events(
        high_watermark=4,
        first_retained_id=1,
        batches=[RuntimeError("database unavailable")],
    )
    timing = Timing()
    stream = FleetStream(
        events,
        Telemetry(),
        Projection(),
        clock=timing.clock,
        monotonic=timing.monotonic,
        sleep=timing.sleep,
    )

    async def read() -> None:
        generator = stream.events(4)
        try:
            with pytest.raises(RuntimeError, match="database unavailable"):
                await anext(generator)
        finally:
            await generator.aclose()

    asyncio.run(read())
    assert events.after_calls == [(4, NOW, 128)]


@dataclass
class Job:
    id: str = "job-1"
    state: str = "queued"


class Jobs:
    def get(self, job_id: str) -> Job:
        return Job(id=job_id)

    def list_page(self, **_kwargs):
        return [], None, 0


class ApiStream:
    def __init__(self) -> None:
        self.calls: list[int | None] = []

    async def events(self, last_event_id: int | None):
        self.calls.append(last_event_id)
        yield "retry: 2000\nid: 12\nevent: fleet-snapshot\ndata: {}\n\n"


def _opaque(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def _browser_client() -> tuple[TestClient, str, str, ApiStream]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            User(
                subject="admin",
                role="administrator",
                disabled_at=None,
                password_verifier=hash_password(PASSWORD),
            )
        )
    browser_auth = BrowserAuthService(
        sessions,
        token_signing_key=b"k" * 32,
        clock=lambda: NOW,
        token_source=iter((_opaque(1), _opaque(2))).__next__,
    )
    browser_session = browser_auth.login("admin", PASSWORD)
    tokens = TokenCodec(b"k" * 32)
    bearer = tokens.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    api_stream = ApiStream()
    app = create_app(
        jobs=Jobs(),
        tokens=tokens,
        audits=MemoryAuditStore(),
        fleet=lambda: {"commit": COMMIT, "nodes": []},
        fleet_projection=Projection(),
        fleet_stream=api_stream,
        now=lambda: 10,
        browser_auth=browser_auth,
    )
    return (
        TestClient(app, base_url="https://forge.example.test"),
        browser_session.token,
        bearer,
        api_stream,
    )


def test_sse_route_requires_cookie_before_header_parse_and_sets_exact_headers() -> None:
    client, browser_session, bearer, stream = _browser_client()
    route = "/api/v1/fleet/stream"

    assert client.get(route, headers={"last-event-id": "+1"}).status_code == 401
    assert client.get(
        route, headers={"authorization": f"Bearer {bearer}"}
    ).status_code == 401
    assert client.get(route, params={"access_token": browser_session}).status_code == 401
    assert stream.calls == []

    client.cookies.set("vonk_session", browser_session)
    assert client.get(route, headers={"last-event-id": "+1"}).status_code == 400
    assert client.get(
        route,
        headers=[("last-event-id", "1"), ("last-event-id", "2")],
    ).status_code == 400
    assert stream.calls == []

    response = client.get(route, headers={"last-event-id": "0"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text == (
        "retry: 2000\nid: 12\nevent: fleet-snapshot\ndata: {}\n\n"
    )
    assert stream.calls == [0]
