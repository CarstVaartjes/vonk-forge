"""Fleet projection: nodes concerns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, TypeAdapter
from vonk_agent_protocol import NodeOfflineReason, ProjectionCode
from vonk_agent_protocol.inventory import NetworkInterface

from ..models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentPresence,
    NodeInventorySnapshot,
)
from ..nas_route_notice import nas_route_notice
from ..recipe_update_notice import RECIPE_UPDATE_AVAILABLE
from ..strict_json import warn_unreadable_once
from ..telemetry import TelemetrySampleView, sustained_low_cpu_clock
from .common import (
    _AGENT_STATE_ADAPTER,
    _CERTIFICATE_OFFLINE_REASONS,
    CapacityReservations,
    FleetNode,
    InstallationPresence,
    InventoryState,
    LoadedPresence,
    NodeConnection,
    ProjectionReason,
    TelemetryState,
    Text64,
    Text256,
    _install_partial_warnings,
    _low_clock_detail,
    _utc,
    telemetry_point,
)

if TYPE_CHECKING:
    from .service import FleetProjection


def _node(
    self: FleetProjection,
    node_id: str,
    profile: AgentNodeProfile | None,
    presence: AgentPresence | None,
    *,
    current: datetime,
    agent: AgentNode,
    certificate: AgentCertificate | None,
    inventory: NodeInventorySnapshot | None,
    telemetry: TelemetrySampleView | None,
    recent_telemetry: Sequence[TelemetrySampleView],
    installed: Sequence[InstallationPresence],
    loaded: Sequence[LoadedPresence],
    reservations: Mapping[str, tuple[int, int]],
    stalls: Sequence[str] = (),
    telemetry_unreadable: bool = False,
) -> FleetNode:
    warnings: list[ProjectionReason] = []
    connection = self._connection(agent, certificate, current)
    inventory_unreadable = False
    try:
        inventory_state = self._inventory(inventory, current)
    except (AttributeError, TypeError, ValueError):
        inventory_state = None
        inventory_unreadable = True
    try:
        telemetry_state = self._telemetry_state(telemetry, current)
    except (AttributeError, TypeError, ValueError):
        telemetry_state = None
        telemetry_unreadable = True
    if connection.online_state != "online":
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.NODE_OFFLINE,
                detail=(
                    "The agent certificate has expired; ordinary mTLS is unavailable. "
                    "Key-proof recovery is authority-bound; fresh enrollment authority "
                    "is available through vonkctl fleet re-enroll."
                    if connection.offline_reason
                    == NodeOfflineReason.CERTIFICATE_EXPIRED
                    else "The authenticated agent is not currently online."
                ),
                severity="warning",
            )
        )
    if inventory_state is None:
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.INVENTORY_MISSING,
                detail=(
                    "Stored admission inventory is unreadable; capacity is unknown."
                    if inventory_unreadable
                    else "No admission inventory snapshot is available."
                ),
                severity="warning",
            )
        )
    elif inventory_state.freshness == "stale":
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.INVENTORY_STALE,
                detail="Admission inventory is stale.",
                severity="warning",
            )
        )
    route_notice = (
        None
        if inventory_state is None
        else nas_route_notice(
            inventory_state.network_interfaces, inventory_state.nas_route_interface
        )
    )
    if route_notice is not None:
        warnings.append(
            ProjectionReason(
                code=route_notice.code,
                detail=route_notice.detail,
                severity="warning",
                recommendation=route_notice.recommendation,
            )
        )
    if telemetry_state is None:
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.TELEMETRY_MISSING,
                detail=(
                    "Stored telemetry is unreadable; measurements are unknown."
                    if telemetry_unreadable
                    else "No telemetry sample is available."
                ),
                severity="warning",
            )
        )
    elif telemetry_state.freshness == "delayed":
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.TELEMETRY_DELAYED,
                detail="Telemetry delivery is delayed.",
                severity="warning",
            )
        )
    elif telemetry_state.freshness == "stale":
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.TELEMETRY_STALE,
                detail="Telemetry is stale.",
                severity="warning",
            )
        )
    if telemetry_state is not None and telemetry_state.freshness != "stale":
        low_clock = sustained_low_cpu_clock(recent_telemetry)
        if low_clock is not None:
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.CPU_LOW_CLOCK,
                    detail=_low_clock_detail(low_clock),
                    severity="warning",
                )
            )
    warnings.extend(_install_partial_warnings(installed))
    for detail in stalls:
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.PROFILE_RETRYING,
                detail=f"A profile load keeps retrying: {detail}"[:256],
                severity="warning",
            )
        )
    for value in loaded:
        if value.projection_issue is not None:
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.RUN_DEGRADED,
                    detail=f"{value.title or value.installation_id}: {value.projection_issue}"[
                        :256
                    ],
                    severity="warning",
                )
            )
    if any(value.healthy is False for value in loaded):
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.RUN_DEGRADED,
                detail="A loaded recipe group is degraded.",
                severity="warning",
            )
        )
    for value in loaded:
        if value.recipe_update is not None:
            warnings.append(
                ProjectionReason(
                    code=RECIPE_UPDATE_AVAILABLE,
                    detail=value.recipe_update.detail,
                    severity="info",
                )
            )
    labels = {} if profile is None else profile.labels
    projection_issues: list[str] = []
    try:
        labels = TypeAdapter(
            Annotated[dict[Text64, Text256], Field(max_length=64)]
        ).validate_python(labels, strict=True)
    except (TypeError, ValueError):
        labels = None
        projection_issues.append(
            "Stored node labels are unreadable; labels are unknown."
        )
        warn_unreadable_once("Fleet node labels", node_id)
    return FleetNode(
        id=node_id,
        display_name=node_id if profile is None else profile.display_name,
        hostname="" if profile is None else profile.hostname,
        ip_address=(None if presence is None else presence.management_address),
        lifecycle="managed" if profile is None else profile.lifecycle,
        labels=labels,
        projection_issues=projection_issues or None,
        connection=connection,
        inventory=inventory_state,
        telemetry=telemetry_state,
        installed=list(installed),
        loaded=list(loaded),
        reservations=CapacityReservations(
            disk_bytes=reservations.get("disk", (0, 0))[0],
            unified_memory_bytes=reservations.get("unified-memory", (0, 0))[0],
            host_memory_bytes=reservations.get("host-memory", (0, 0))[0],
            gpu_memory_bytes=reservations.get("gpu-memory", (0, 0))[0],
            port_count=reservations.get("port", (0, 0))[1],
        ),
        warnings=warnings,
    )


def _connection(
    self: FleetProjection,
    value: AgentNode | None,
    certificate: AgentCertificate | None,
    current: datetime,
) -> NodeConnection:
    certificate_state = self._certificate_state(certificate, current)
    if value is None:
        return NodeConnection(
            agent_state="unregistered",
            certificate_state=certificate_state,
            online_state="unregistered",
            offline_reason=NodeOfflineReason.UNREGISTERED,
            last_seen_at=None,
            last_seen_age_seconds=None,
        )
    last_seen = None if value.last_seen_at is None else _utc(value.last_seen_at)
    age = None if last_seen is None else max(0.0, (current - last_seen).total_seconds())
    if value.state == "revoked" or value.revoked_at is not None:
        offline_reason: NodeOfflineReason | None = NodeOfflineReason.AGENT_REVOKED
    elif value.state != "active":
        offline_reason = NodeOfflineReason.AGENT_INACTIVE
    elif certificate_state != "valid":
        offline_reason = _CERTIFICATE_OFFLINE_REASONS[certificate_state]
    elif last_seen is None:
        offline_reason = NodeOfflineReason.NEVER_SEEN
    elif current - last_seen < timedelta(0):
        offline_reason = NodeOfflineReason.LAST_SEEN_IN_FUTURE
    elif current - last_seen > timedelta(seconds=self._agent_online_seconds):
        offline_reason = NodeOfflineReason.STALE
    else:
        offline_reason = None
    return NodeConnection(
        agent_state=_AGENT_STATE_ADAPTER.validate_python(value.state, strict=True),
        certificate_state=certificate_state,
        online_state="online" if offline_reason is None else "offline",
        offline_reason=offline_reason,
        last_seen_at=last_seen,
        last_seen_age_seconds=age,
    )


def _inventory(
    self: FleetProjection, value: NodeInventorySnapshot | None, current: datetime
) -> InventoryState | None:
    if value is None:
        return None
    observed = _utc(value.observed_at)
    age = max(0.0, (current - observed).total_seconds())
    return InventoryState(
        observed_at=observed,
        received_at=_utc(value.received_at),
        age_seconds=age,
        freshness="stale" if age > self._inventory_fresh_seconds else "fresh",
        disk_total_bytes=value.disk_total_bytes,
        disk_free_bytes=value.disk_free_bytes,
        host_memory_total_bytes=value.host_memory_total_bytes,
        host_memory_free_bytes=value.host_memory_free_bytes,
        gpu_memory_total_bytes=value.gpu_memory_total_bytes,
        gpu_memory_free_bytes=value.gpu_memory_free_bytes,
        gpu_count=value.gpu_count,
        artifact_store_read_only=value.artifact_store_read_only,
        capabilities=list(value.capabilities),
        fabric_address=value.fabric_address,
        fabric_bandwidth_mbps=value.fabric_bandwidth_mbps,
        nvidia_driver_version=value.nvidia_driver_version,
        container_runtime_version=value.container_runtime_version,
        network_interfaces=(
            None
            if value.network_interfaces is None
            else [NetworkInterface(**item) for item in value.network_interfaces]
        ),
        nas_route_interface=value.nas_route_interface,
    )


def _telemetry_state(
    self: FleetProjection, value: TelemetrySampleView | None, current: datetime
) -> TelemetryState | None:
    if value is None:
        return None
    age = max(0.0, (current - _utc(value.observed_at)).total_seconds())
    if age <= self._telemetry_live_seconds:
        freshness = "live"
    elif age <= self._telemetry_delayed_seconds:
        freshness = "delayed"
    else:
        freshness = "stale"
    return TelemetryState(
        age_seconds=age,
        freshness=freshness,
        sample=telemetry_point(value),
    )
