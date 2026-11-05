"""Telemetry and inventory writes never hold the agent_nodes row.

Production audit 2026-11-05: telemetry (every ~10 s) and inventory (every 120 s)
selected the node FOR UPDATE. That refused admission's FOR NO KEY UPDATE NOWAIT
and queued the foreign-key KEY SHARE of every other agent child insert behind
the open transaction. Both writes are now idempotent inserts keyed by their own
identity; this holds a write's transaction open and proves nothing waits on it.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.admission_locking import (
    AdmissionRowLock,
    lock_admission_rows,
    set_admission_timeout,
)
from vonk_control.agent_contacts import record_contact, record_observation_delivery
from vonk_control.inventory_repository import InventoryRepository, NodeInventoryReport
from vonk_control.models import (
    AgentNode,
    AgentNodeContact,
    Base,
    NodeTelemetryLatest,
)
from vonk_control.telemetry import TelemetryRepository, TelemetrySample

NODE = "spk_" + "a" * 32
BOOT = uuid.UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _sample(observed_at: datetime) -> TelemetrySample:
    return TelemetrySample.model_validate_json(
        canonical_message(
            {
                "boot_id": str(BOOT),
                "observed_at": observed_at,
                "memory_total_bytes": None,
                "memory_available_bytes": None,
                "disk_total_bytes": None,
                "disk_free_bytes": None,
                "gpu_utilization_percent": None,
                "gpu_memory_total_bytes": None,
                "gpu_memory_free_bytes": None,
            }
        )
    )


@pytest.fixture
def fleet(postgres_engine: Engine):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active"))
        session.add(AgentNodeContact(node_id=NODE))
    return postgres_engine, sessions


def _admit(sessions) -> None:
    """Capacity admission's NOWAIT node lock."""
    with sessions.begin() as session:
        set_admission_timeout(session)
        lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "target-agent-nodes",
                    AgentNode,
                    select(AgentNode).where(AgentNode.node_id == NODE),
                ),
            ),
        )


def _child_insert(sessions) -> float:
    """An agent child insert (foreign key to the node); returns its wait."""
    started = time.monotonic()
    with sessions.begin() as session:
        session.execute(text("SELECT set_config('lock_timeout', '2000ms', true)"))
        record_contact(session, NODE, last_seen_at=NOW, protocol_version=1)
        record_observation_delivery(session, NODE, 204, NOW)
    return time.monotonic() - started


def test_open_telemetry_write_blocks_neither_admission_nor_child_inserts(fleet):
    engine, sessions = fleet
    repository = TelemetryRepository(sessions, clock=lambda: NOW)
    inserted, release = threading.Event(), threading.Event()
    writer = threading.local()

    def hold(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if getattr(writer, "active", False) and statement.startswith(
            "INSERT INTO node_telemetry_latest"
        ):
            inserted.set()
            assert release.wait(30)

    event.listen(engine, "after_cursor_execute", hold)

    def write() -> None:
        writer.active = True
        repository.record_batch(NODE, (_sample(NOW),))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            assert inserted.wait(30), "telemetry write never reached its insert"
            try:
                _admit(sessions)
                assert _child_insert(sessions) < 1.5
            finally:
                release.set()
            future.result(timeout=30)
    finally:
        event.remove(engine, "after_cursor_execute", hold)

    with sessions() as session:
        assert session.get(NodeTelemetryLatest, NODE) is not None


def _report(observed_at: datetime) -> NodeInventoryReport:
    return NodeInventoryReport.model_validate_json(
        canonical_message(
            {
                "node_id": NODE,
                "observed_at": observed_at,
                "disk_total_bytes": 1000,
                "disk_free_bytes": 700,
                "host_memory_total_bytes": 500,
                "host_memory_free_bytes": 300,
                "gpu_memory_total_bytes": 400,
                "gpu_memory_free_bytes": 250,
                "gpu_count": 1,
                "artifact_store_read_only": False,
                "capabilities": ("runtime.vllm.v1",),
                "memory_pool": "separate",
                "schema_version": 1,
                "nvidia_driver_version": "unknown",
                "container_runtime_version": "unknown",
            }
        )
    )


def test_open_inventory_write_blocks_neither_admission_nor_child_inserts(fleet):
    engine, sessions = fleet
    repository = InventoryRepository(sessions, clock=lambda: NOW)
    inserted, release = threading.Event(), threading.Event()
    writer = threading.local()

    def hold(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if getattr(writer, "active", False) and statement.startswith(
            "INSERT INTO node_inventory_snapshots"
        ):
            inserted.set()
            assert release.wait(30)

    event.listen(engine, "after_cursor_execute", hold)

    def write() -> None:
        writer.active = True
        repository.record(_report(NOW))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            assert inserted.wait(30), "inventory write never reached its insert"
            try:
                _admit(sessions)
                assert _child_insert(sessions) < 1.5
            finally:
                release.set()
            future.result(timeout=30)
    finally:
        event.remove(engine, "after_cursor_execute", hold)

    assert repository.snapshot_count(NODE) == 1


def test_concurrent_identical_inventory_twin_returns_the_stored_row(fleet):
    engine, sessions = fleet
    repository = InventoryRepository(sessions, clock=lambda: NOW)
    inserted, release = threading.Event(), threading.Event()
    writer = threading.local()

    def hold(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if getattr(writer, "first", False) and statement.startswith(
            "INSERT INTO node_inventory_snapshots"
        ):
            inserted.set()
            assert release.wait(30)

    event.listen(engine, "after_cursor_execute", hold)

    def first() -> str:
        writer.first = True
        return repository.record(_report(NOW)).id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(first)
            assert inserted.wait(30)
            twin = pool.submit(repository.record, _report(NOW))
            time.sleep(0.5)  # the twin now waits on the open identical insert
            release.set()
            winner_id = winner.result(timeout=30)
            twin_row = twin.result(timeout=30)
    finally:
        event.remove(engine, "after_cursor_execute", hold)

    assert twin_row.id == winner_id
    assert repository.snapshot_count(NODE) == 1
