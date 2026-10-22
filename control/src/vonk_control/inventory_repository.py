"""Append-only authenticated GPU node inventory evidence."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.inventory import (
    HostSettings,
    InstallationBytes,
    InstallationId,
    InventoryRequest,
    MemoryPool,
    ModelStoreObject,
    NetworkInterface,
    RunScopeId,
)

from .installed_bytes import reconcile_missing_installations, refresh_installed_bytes
from .logging import log_event
from .models import AgentNode, NodeInventorySnapshot

_LOG = logging.getLogger(__name__)

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
    network_interfaces: tuple[NetworkInterface, ...] | None = None
    nas_route_interface: str | None = None
    host_settings: HostSettings | None = None
    unused_model_bytes: int | None = None
    model_objects: list[ModelStoreObject] | None = None
    installations: list[InstallationId] | None = None
    runs: list[RunScopeId] | None = None
    installation_bytes: list[InstallationBytes] | None = None


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
    network_interfaces: tuple[NetworkInterface, ...] | None = field(
        kw_only=True, default=None
    )
    nas_route_interface: str | None = field(kw_only=True, default=None)
    host_settings: HostSettings | None = field(kw_only=True, default=None)
    unused_model_bytes: int | None = field(kw_only=True, default=None)
    model_objects: list[ModelStoreObject] | None = field(kw_only=True, default=None)
    installations: list[InstallationId] | None = field(kw_only=True, default=None)
    runs: list[RunScopeId] | None = field(kw_only=True, default=None)
    installation_bytes: list[InstallationBytes] | None = field(
        kw_only=True, default=None
    )


def _validated_inventory(
    value: InventorySnapshotInput | NodeInventorySnapshot, *, observed_at: datetime
) -> InventoryRequest:
    document = {
        name: getattr(value, name)
        for name in InventoryRequest.model_fields
        if name
        not in {
            "schema_version",
            "observed_at",
        }
    }
    interfaces = document["network_interfaces"]
    if interfaces is not None:
        document["network_interfaces"] = [
            item.model_dump(mode="json") if isinstance(item, NetworkInterface) else item
            for item in interfaces
        ]
    if isinstance(document["host_settings"], HostSettings):
        document["host_settings"] = document["host_settings"].model_dump(mode="json")
    objects = document["model_objects"]
    if isinstance(objects, list):
        document["model_objects"] = [
            item.model_dump(mode="json") if isinstance(item, ModelStoreObject) else item
            for item in objects
        ]
    document.update(schema_version=1, observed_at=observed_at.isoformat())
    validated = InventoryRequest.model_validate_json(canonical_message(document))
    for warning in validated.evidence_warnings:
        log_event(
            _LOG,
            "agent.evidence_dropped",
            service="control-api",
            code=warning.value,
            endpoint="inventory",
            node_id=value.node_id,
        )
    return validated


class InventoryRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._clock = sessions, clock

    def record(
        self, value: InventorySnapshotInput, *, report: InventoryRequest | None = None
    ) -> NodeInventorySnapshot:
        validated = _validated_inventory(value, observed_at=value.observed_at)
        if report is not None:
            # Persist the canonical report directory items alongside capacity.
            validated = validated.model_copy(
                update={
                    "installations": report.installations,
                    "runs": report.runs,
                    "installation_bytes": report.installation_bytes,
                }
            )
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
            fabric_address=validated.fabric_address,
            fabric_bandwidth_mbps=validated.fabric_bandwidth_mbps,
            nvidia_driver_version=value.nvidia_driver_version,
            container_runtime_version=value.container_runtime_version,
            network_interfaces=(
                None
                if validated.network_interfaces is None
                else [
                    item.model_dump(mode="json")
                    for item in validated.network_interfaces
                ]
            ),
            nas_route_interface=validated.nas_route_interface,
            host_settings=(
                None
                if validated.host_settings is None
                else validated.host_settings.model_dump(mode="json")
            ),
            model_objects=None
            if validated.model_objects is None
            else [
                {"sha256": item.sha256, "bytes": item.bytes}
                for item in validated.model_objects
            ],
            unused_model_bytes=validated.unused_model_bytes,
            installations=validated.installations,
            runs=validated.runs,
            installation_bytes=validated.installation_bytes,
            evidence_digest=digest,
        )
        with self._sessions.begin() as session:
            session.execute(
                select(AgentNode.node_id)
                .where(AgentNode.node_id == value.node_id)
                .with_for_update(of=AgentNode)
            ).one()
            existing = session.scalar(
                select(NodeInventorySnapshot).where(
                    NodeInventorySnapshot.node_id == value.node_id,
                    NodeInventorySnapshot.observed_at == value.observed_at,
                )
            )
            if existing is not None:
                if existing.evidence_digest != digest:
                    raise ValueError(
                        "inventory identity already binds different evidence"
                    )
                return existing
            latest = session.scalar(
                select(NodeInventorySnapshot.observed_at)
                .where(NodeInventorySnapshot.node_id == value.node_id)
                .order_by(NodeInventorySnapshot.observed_at.desc())
                .limit(1)
            )
            retained_report = _validated_inventory(row, observed_at=value.observed_at)
            if latest is None or value.observed_at >= (
                latest if latest.tzinfo is not None else latest.replace(tzinfo=UTC)
            ):
                reconcile_missing_installations(
                    session,
                    value.node_id,
                    retained_report.installations,
                    observed_at=value.observed_at,
                    before=value.observed_at - MAX_INVENTORY_FUTURE_SKEW,
                )
                if isinstance(row.installation_bytes, list):
                    refresh_installed_bytes(
                        session, value.node_id, row.installation_bytes
                    )
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
                network_interfaces=(
                    None
                    if validated.network_interfaces is None
                    else tuple(validated.network_interfaces)
                ),
                nas_route_interface=validated.nas_route_interface,
                host_settings=validated.host_settings,
                model_objects=validated.model_objects,
                unused_model_bytes=validated.unused_model_bytes,
                installations=validated.installations,
                runs=validated.runs,
                installation_bytes=validated.installation_bytes,
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
