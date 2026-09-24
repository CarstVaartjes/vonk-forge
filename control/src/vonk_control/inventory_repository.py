"""Append-only authenticated GPU node inventory evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.inventory import InventoryRequest, MemoryPool

from .models import NodeInventorySnapshot

# Agent inventory may lead the Controller clock by this much. Consumers that
# order it against Controller-owned events must retain the same uncertainty.
MAX_INVENTORY_FUTURE_SKEW = timedelta(seconds=30)


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
    memory_pool: MemoryPool = field(kw_only=True)
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
    memory_pool: MemoryPool = field(kw_only=True)


def _validated_inventory(
    value: InventorySnapshotInput | NodeInventorySnapshot, *, observed_at: datetime
) -> InventoryRequest:
    document = {
        name: getattr(value, name)
        for name in InventoryRequest.model_fields
        if name not in {"schema_version", "observed_at"}
    }
    document.update(schema_version=1, observed_at=observed_at.isoformat())
    return InventoryRequest.model_validate_json(canonical_message(document))


class InventoryRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._clock = sessions, clock

    def record(self, value: InventorySnapshotInput) -> NodeInventorySnapshot:
        validated = _validated_inventory(value, observed_at=value.observed_at)
        document = validated.model_dump(mode="json", exclude={"schema_version"})
        document["node_id"] = value.node_id
        document["capabilities"] = sorted(validated.capabilities)
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
            memory_pool=validated.memory_pool,
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
            # Validate the persisted JSON semantics using the producer's current
            # contract. A typed annotation or SQL string alone is not evidence.
            validated = _validated_inventory(row, observed_at=observed)
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
                tuple(validated.capabilities),
                row.evidence_digest,
                (now - observed).total_seconds() > maximum_age,
                row.fabric_address,
                row.fabric_bandwidth_mbps,
                row.nvidia_driver_version,
                row.container_runtime_version,
                memory_pool=validated.memory_pool,
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
