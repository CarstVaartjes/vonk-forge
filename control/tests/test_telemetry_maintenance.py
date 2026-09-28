from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.fleet_events import FleetEventRepository
from vonk_control.fleet_projection import FleetProjection
from vonk_control.fleet_stream import FleetStream
from vonk_control.models import (
    AgentNode,
    Base,
    FleetEventCursor,
    FleetStreamEvent,
    NodeTelemetryLatest,
    NodeTelemetrySample,
)
from vonk_control.telemetry import (
    TelemetryRepository,
)

NODE_A = "spk_" + "a" * 32
BOOT_A = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 8, 15, 12, 4, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'maintenance.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
    return factory


def _raw(
    identifier: str,
    observed_at: datetime,
    *,
    sequence: int,
    cpu: float | None = None,
) -> NodeTelemetrySample:
    return NodeTelemetrySample(
        id=identifier,
        node_id=NODE_A,
        boot_id=BOOT_A,
        observed_at=observed_at,
        received_at=observed_at,
        gpu_utilization_percent=cpu,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
    )


def test_run_once_captures_one_aware_clock_value_and_rejects_unbounded_limits(
    sessions,
) -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    maintenance = telemetry_maintenance.TelemetryMaintenance(sessions, clock=clock)
    maintenance.run_once()
    assert calls == 1

    for delete_limit in (0, 25_001):
        with pytest.raises(ValueError, match="limit"):
            maintenance.run_once(delete_limit=delete_limit)
    assert calls == 1

    with pytest.raises(ValueError, match="timezone-aware"):
        telemetry_maintenance.TelemetryMaintenance(
            sessions,
            clock=lambda: NOW.replace(tzinfo=None),
        ).run_once()


def test_retention_boundaries_are_strict_ordered_and_repeatedly_bounded(
    sessions,
) -> None:
    raw_cutoff = NOW - timedelta(hours=24)
    with sessions.begin() as session:
        session.add_all(
            (
                _raw(
                    "raw-old-1",
                    raw_cutoff - timedelta(seconds=2),
                    sequence=1,
                ),
                _raw(
                    "raw-old-2",
                    raw_cutoff - timedelta(seconds=1),
                    sequence=2,
                ),
                _raw("raw-boundary", raw_cutoff, sequence=3),
                FleetStreamEvent(
                    id=1,
                    event_type="operation-state",
                    node_id=None,
                    entity_kind="job",
                    entity_id="job-1",
                    payload={"schema_version": 1},
                    occurred_at=NOW - timedelta(hours=1),
                    expires_at=NOW - timedelta(seconds=1),
                ),
                FleetStreamEvent(
                    id=2,
                    event_type="operation-state",
                    node_id=None,
                    entity_kind="job",
                    entity_id="job-2",
                    payload={"schema_version": 1},
                    occurred_at=NOW - timedelta(hours=1),
                    expires_at=NOW,
                ),
                FleetStreamEvent(
                    id=3,
                    event_type="operation-state",
                    node_id=None,
                    entity_kind="job",
                    entity_id="job-3",
                    payload={"schema_version": 1},
                    occurred_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(seconds=1),
                ),
            )
        )
        session.execute(update(FleetEventCursor).values(last_id=3))

    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    )
    maintenance.run_once(delete_limit=1)

    with sessions() as session:
        assert session.scalars(
            select(NodeTelemetrySample.id).order_by(NodeTelemetrySample.observed_at)
        ).all() == ["raw-old-2", "raw-boundary"]
        assert session.scalars(
            select(FleetStreamEvent.id).order_by(FleetStreamEvent.id)
        ).all() == [2, 3]
        assert session.get(FleetEventCursor, 1).last_id == 3

    maintenance.run_once(delete_limit=1)

    with sessions() as session:
        assert session.scalars(select(NodeTelemetrySample.id)).all() == ["raw-boundary"]
        assert session.scalars(select(FleetStreamEvent.id)).all() == [3]
        assert session.get(FleetEventCursor, 1).last_id == 3


def test_latest_raw_pruning_appends_authoritative_missing_sample_reset(
    sessions,
) -> None:
    sample_id = "latest-expired"
    with sessions.begin() as session:
        session.add(
            _raw(
                sample_id,
                NOW - timedelta(hours=24, microseconds=1),
                sequence=1,
                cpu=10,
            )
        )
        session.add(NodeTelemetryLatest(node_id=NODE_A, sample_id=sample_id))

    telemetry_maintenance.TelemetryMaintenance(sessions, clock=lambda: NOW).run_once(
        delete_limit=1
    )

    with sessions() as session:
        assert session.get(NodeTelemetryLatest, NODE_A) is None
        assert session.get(NodeTelemetrySample, sample_id) is None
        event = session.get(FleetStreamEvent, 1)
        assert event is not None
        assert (
            event.event_type,
            event.node_id,
            event.entity_kind,
            event.entity_id,
            event.payload,
        ) == (
            "node-telemetry",
            NODE_A,
            "node-telemetry-latest",
            NODE_A,
            {
                "node_id": NODE_A,
                "sample_id": sample_id,
            },
        )
        assert event.occurred_at.replace(tzinfo=UTC) == NOW
        assert event.expires_at.replace(tzinfo=UTC) == NOW + timedelta(hours=24)
        assert session.get(FleetEventCursor, 1).last_id == 1
    assert TelemetryRepository(sessions, clock=lambda: NOW).latest((NODE_A,)) == {}

    class Repository:
        def head(self) -> str:
            return "a" * 64

        def read_document(self, commit: str, path: str) -> SimpleNamespace:
            raise AssertionError(f"unexpected document read: {commit} {path}")

    events = FleetEventRepository(sessions, clock=lambda: NOW)
    telemetry = TelemetryRepository(sessions, clock=lambda: NOW)
    projection = FleetProjection(
        sessions,
        clock=lambda: NOW,
        events=events,
        telemetry=telemetry,
    )

    stream = FleetStream(
        events,
        telemetry,
        projection,
        clock=lambda: NOW,
    )

    async def read_reset() -> str:
        generator = stream.events(0)
        try:
            return await anext(generator)
        finally:
            assert isinstance(generator, AsyncGenerator)
            await generator.aclose()

    frame = asyncio.run(read_reset())
    fields = {
        key: value
        for key, value in (
            line.split(": ", 1) for line in frame.splitlines() if ": " in line
        )
    }
    data = json.loads(fields["data"])
    assert fields["id"] == "1"
    assert fields["event"] == "fleet-snapshot"
    assert data["reset_reason"] == "missing-telemetry-sample"
    assert data["snapshot"]["event_cursor"] == 1
    assert [node["id"] for node in data["snapshot"]["nodes"]] == [NODE_A]
    assert data["snapshot"]["nodes"][0]["telemetry"] is None
