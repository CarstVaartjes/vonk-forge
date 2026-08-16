"""Append-only authenticated GPU node inventory evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import NodeInventorySnapshot


@dataclass(frozen=True, slots=True)
class InventorySnapshotInput:
    node_id: str
    observed_at: datetime
    disk_total_bytes: int
    disk_free_bytes: int
    host_memory_total_bytes: int
    host_memory_free_bytes: int
    gpu_memory_total_bytes: int
    gpu_memory_free_bytes: int
    gpu_count: int
    artifact_store_read_only: bool
    capabilities: tuple[str, ...]
    fabric_address: str | None = None
    fabric_bandwidth_mbps: int | None = None
    nvidia_driver_version: str = "unknown"
    container_runtime_version: str = "unknown"


@dataclass(frozen=True, slots=True)
class InventorySnapshotView:
    id: str
    node_id: str
    observed_at: datetime
    received_at: datetime
    disk_total_bytes: int
    disk_free_bytes: int
    host_memory_total_bytes: int
    host_memory_free_bytes: int
    gpu_memory_total_bytes: int
    gpu_memory_free_bytes: int
    gpu_count: int
    artifact_store_read_only: bool
    capabilities: tuple[str, ...]
    evidence_digest: str
    stale: bool
    fabric_address: str | None
    fabric_bandwidth_mbps: int | None
    nvidia_driver_version: str
    container_runtime_version: str


class InventoryRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._clock = sessions, clock

    def record(self, value: InventorySnapshotInput) -> NodeInventorySnapshot:
        if (
            value.observed_at.tzinfo is None
            or len(value.capabilities) != len(set(value.capabilities))
            or not all(
                capability and len(capability) <= 128
                for capability in value.capabilities
            )
        ):
            raise ValueError("inventory evidence is invalid")
        if (value.fabric_address is None) != (value.fabric_bandwidth_mbps is None):
            raise ValueError("inventory fabric evidence is incomplete")
        if value.fabric_address is not None:
            try:
                address = ipaddress.ip_address(value.fabric_address)
            except ValueError as error:
                raise ValueError("inventory fabric address is invalid") from error
            if (
                str(address) != value.fabric_address
                or not value.fabric_bandwidth_mbps
                or value.fabric_bandwidth_mbps > 1_000_000
            ):
                raise ValueError("inventory fabric address is invalid")
        if any(
            not value or len(value) > 256 or not value.isascii()
            for value in (value.nvidia_driver_version, value.container_runtime_version)
        ):
            raise ValueError("inventory runtime evidence is invalid")
        document = asdict(value)
        document["observed_at"] = value.observed_at.isoformat()
        document["capabilities"] = sorted(value.capabilities)
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row = NodeInventorySnapshot(
            node_id=value.node_id,
            observed_at=value.observed_at,
            received_at=self._clock(),
            disk_total_bytes=value.disk_total_bytes,
            disk_free_bytes=value.disk_free_bytes,
            host_memory_total_bytes=value.host_memory_total_bytes,
            host_memory_free_bytes=value.host_memory_free_bytes,
            gpu_memory_total_bytes=value.gpu_memory_total_bytes,
            gpu_memory_free_bytes=value.gpu_memory_free_bytes,
            gpu_count=value.gpu_count,
            artifact_store_read_only=value.artifact_store_read_only,
            capabilities=sorted(value.capabilities),
            fabric_address=value.fabric_address,
            fabric_bandwidth_mbps=value.fabric_bandwidth_mbps,
            nvidia_driver_version=value.nvidia_driver_version,
            container_runtime_version=value.container_runtime_version,
            evidence_digest=digest,
        )
        with self._sessions.begin() as session:
            session.add(row)
        return row

    def latest(
        self,
        node_id: str,
        *,
        now: datetime,
        maximum_age: int,
        _session: Session | None = None,
    ) -> InventorySnapshotView:
        with (
            nullcontext(_session) if _session is not None else self._sessions()
        ) as session:
            row = session.scalar(
                select(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.node_id == node_id)
                .order_by(NodeInventorySnapshot.observed_at.desc())
                .limit(1)
            )
            if row is None:
                raise KeyError(node_id)
            observed = (
                row.observed_at
                if row.observed_at.tzinfo
                else row.observed_at.replace(tzinfo=UTC)
            )
            return InventorySnapshotView(
                row.id,
                row.node_id,
                observed,
                row.received_at,
                row.disk_total_bytes,
                row.disk_free_bytes,
                row.host_memory_total_bytes,
                row.host_memory_free_bytes,
                row.gpu_memory_total_bytes,
                row.gpu_memory_free_bytes,
                row.gpu_count,
                row.artifact_store_read_only,
                tuple(row.capabilities),
                row.evidence_digest,
                (now - observed).total_seconds() > maximum_age,
                row.fabric_address,
                row.fabric_bandwidth_mbps,
                row.nvidia_driver_version,
                row.container_runtime_version,
            )

    def snapshot_count(self, node_id: str) -> int:
        with self._sessions() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(NodeInventorySnapshot)
                    .where(NodeInventorySnapshot.node_id == node_id)
                )
                or 0
            )
