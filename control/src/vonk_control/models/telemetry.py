"""Models: telemetry."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Connection,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import ModelFileState, machine_check
from vonk_agent_protocol.inventory import MemoryPool

from ..model_primitives import Base, _lower_hex, _Utf8ByteLength, _uuid_shape


class NodeInventorySnapshot(Base):
    __tablename__ = "node_inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("node_id", "observed_at", name="uq_inventory_node_observed"),
        CheckConstraint(
            "disk_total_bytes>=0 AND disk_free_bytes>=0 AND disk_free_bytes<=disk_total_bytes",
            name="ck_inventory_disk",
        ),
        CheckConstraint(
            "host_memory_total_bytes>=0 AND host_memory_free_bytes>=0 AND host_memory_free_bytes<=host_memory_total_bytes",
            name="ck_inventory_host_memory",
        ),
        CheckConstraint(
            "gpu_memory_total_bytes>=0 AND gpu_memory_free_bytes>=0 AND gpu_memory_free_bytes<=gpu_memory_total_bytes AND gpu_count>=0",
            name="ck_inventory_gpu_memory",
        ),
        CheckConstraint(
            "(fabric_address IS NULL AND fabric_bandwidth_mbps IS NULL) OR (fabric_address IS NOT NULL AND fabric_bandwidth_mbps>0)",
            name="ck_inventory_fabric",
        ),
        CheckConstraint(_lower_hex("evidence_digest", 64), name="ck_inventory_digest"),
        CheckConstraint(
            "memory_pool IN ('shared','separate') AND (memory_pool!='shared' OR gpu_count>0)",
            name="ck_inventory_memory_pool",
        ),
        Index("ix_inventory_node_observed", "node_id", "observed_at"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    disk_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    host_memory_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    host_memory_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_memory_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_memory_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_pool: Mapped[MemoryPool] = mapped_column(String(16), nullable=False)
    fabric_address: Mapped[str | None] = mapped_column(String(45))
    fabric_bandwidth_mbps: Mapped[int | None] = mapped_column(BigInteger)
    # NULL: the agent did not report network evidence (unknown, not "none").
    network_interfaces: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    nas_route_interface: Mapped[str | None] = mapped_column(String(15))
    nvidia_driver_version: Mapped[str] = mapped_column(
        String(256), nullable=False, default="unknown", server_default="unknown"
    )
    container_runtime_version: Mapped[str] = mapped_column(
        String(256), nullable=False, default="unknown", server_default="unknown"
    )
    artifact_store_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )


class NodeTelemetrySample(Base):
    __tablename__ = "node_telemetry_samples"
    __table_args__ = (
        UniqueConstraint("node_id", "id", name="uq_telemetry_node_sample"),
        UniqueConstraint(
            "node_id",
            "boot_id",
            "observed_at",
            name="uq_telemetry_node_boot_observed",
        ),
        CheckConstraint(_uuid_shape("boot_id"), name="ck_telemetry_boot_id_shape"),
        CheckConstraint(
            "gpu_utilization_percent IS NULL OR "
            "gpu_utilization_percent BETWEEN 0 AND 100",
            name="ck_telemetry_utilization",
        ),
        CheckConstraint(
            "(memory_total_bytes IS NULL AND memory_available_bytes IS NULL) OR "
            "(memory_total_bytes IS NOT NULL AND memory_available_bytes IS NOT NULL AND "
            "memory_total_bytes >= 0 AND memory_available_bytes >= 0 AND "
            "memory_total_bytes <= 17592186044416 AND "
            "memory_available_bytes <= 17592186044416 AND "
            "memory_available_bytes <= memory_total_bytes)",
            name="ck_telemetry_memory",
        ),
        CheckConstraint(
            "(disk_total_bytes IS NULL AND disk_free_bytes IS NULL) OR "
            "(disk_total_bytes IS NOT NULL AND disk_free_bytes IS NOT NULL AND "
            "disk_total_bytes >= 0 AND disk_free_bytes >= 0 AND "
            "disk_total_bytes <= 17592186044416 AND "
            "disk_free_bytes <= 17592186044416 AND "
            "disk_free_bytes <= disk_total_bytes)",
            name="ck_telemetry_disk",
        ),
        CheckConstraint(
            "(gpu_memory_total_bytes IS NULL AND gpu_memory_free_bytes IS NULL) OR "
            "(gpu_memory_total_bytes IS NOT NULL AND gpu_memory_free_bytes IS NOT NULL AND "
            "gpu_memory_total_bytes >= 0 AND gpu_memory_free_bytes >= 0 AND "
            "gpu_memory_total_bytes <= 17592186044416 AND "
            "gpu_memory_free_bytes <= 17592186044416 AND "
            "gpu_memory_free_bytes <= gpu_memory_total_bytes)",
            name="ck_telemetry_gpu_memory",
        ),
        Index("ix_telemetry_node_observed", "node_id", "observed_at"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    boot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    memory_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    memory_available_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_utilization_percent: Mapped[float | None] = mapped_column(Float)
    gpu_memory_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_memory_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_unavailable_reason: Mapped[str | None] = mapped_column(String(64))
    gpu_temperature_c: Mapped[int | None] = mapped_column(Integer)
    cpu_frequency_avg_mhz: Mapped[int | None] = mapped_column(Integer)
    cpu_frequency_min_mhz: Mapped[int | None] = mapped_column(Integer)
    cpu_frequency_max_mhz: Mapped[int | None] = mapped_column(Integer)


class NodeTelemetryLatest(Base):
    __tablename__ = "node_telemetry_latest"
    __table_args__ = (
        ForeignKeyConstraint(
            ("node_id", "sample_id"),
            ("node_telemetry_samples.node_id", "node_telemetry_samples.id"),
            name="fk_telemetry_latest_node_sample",
            ondelete="RESTRICT",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    sample_id: Mapped[str] = mapped_column(
        nullable=False,
        unique=True,
    )


class FleetEventCursor(Base):
    __tablename__ = "fleet_event_cursor"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_fleet_event_cursor_singleton"),
        CheckConstraint("last_id >= 0", name="ck_fleet_event_cursor_last_id"),
    )
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


@event.listens_for(FleetEventCursor.__table__, "after_create")
def _seed_fleet_event_cursor(target: Table, connection: Connection, **_kw) -> None:
    connection.execute(target.insert().values(singleton_id=1, last_id=0))


class FleetStreamEvent(Base):
    __tablename__ = "fleet_stream_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('node-telemetry','node-profile','recipe-state','operation-state')",
            name="ck_fleet_stream_events_event_type",
        ),
        CheckConstraint(
            "expires_at > occurred_at", name="ck_fleet_stream_events_expiry"
        ),
        CheckConstraint(
            _Utf8ByteLength(literal_column("payload")).between(2, 8192),
            name="ck_fleet_stream_events_payload_size",
        ),
        Index("ix_fleet_stream_events_expires_id", "expires_at", "id"),
        Index("ix_fleet_stream_events_node_id", "node_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(36))
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class NodeArtifact(Base):
    __tablename__ = "node_artifacts"
    __table_args__ = (
        UniqueConstraint("node_id", "digest", name="uq_node_artifact_digest"),
        CheckConstraint(
            "kind IN ('image','image-layer','model','auxiliary')",
            name="ck_node_artifacts_kind",
        ),
        CheckConstraint(
            machine_check(ModelFileState),
            name="ck_node_artifacts_state",
        ),
        CheckConstraint(
            "size_bytes>=0 AND ref_count>=0", name="ck_node_artifacts_sizes"
        ),
        CheckConstraint(_lower_hex("digest", 64), name="ck_node_artifacts_digest"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    ref_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
