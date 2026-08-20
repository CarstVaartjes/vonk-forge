"""Read-only projection joining PostgreSQL Fleet authority with observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .metrics import protocol_version_bucket
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    NodeInventorySnapshot,
    Observation,
)


class DashboardService:
    def __init__(
        self,
        authority,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        protocol_minimum: int = 3,
        protocol_maximum: int = 3,
        agent_online_window_seconds: int = 150,
        health_stale_after_seconds: int = 300,
        inventory_stale_after_seconds: int = 300,
    ) -> None:
        if protocol_minimum < 1 or protocol_maximum < protocol_minimum:
            raise ValueError("supported protocol range is invalid")
        if (
            agent_online_window_seconds <= 0
            or health_stale_after_seconds <= 0
            or inventory_stale_after_seconds <= 0
        ):
            raise ValueError("observation windows must be positive")
        self._authority = authority
        self._sessions = sessions
        self._clock = clock
        self._protocol_minimum = protocol_minimum
        self._protocol_maximum = protocol_maximum
        self._agent_online_window_seconds = agent_online_window_seconds
        self._health_stale_after_seconds = health_stale_after_seconds
        self._inventory_stale_after_seconds = inventory_stale_after_seconds

    def fleet(self) -> dict[str, object]:
        revision = self._authority.head()
        with self._sessions() as session:
            agent_nodes = {
                node.node_id: node
                for node in session.scalars(
                    select(AgentNode)
                    .where(
                        AgentNode.state != "revoked",
                        AgentNode.revoked_at.is_(None),
                    )
                    .order_by(AgentNode.node_id)
                )
            }
            fleet_node_ids = tuple(agent_nodes)
            profiles = {
                profile.node_id: profile
                for profile in session.scalars(
                    select(AgentNodeProfile)
                    .where(AgentNodeProfile.node_id.in_(fleet_node_ids))
                    .order_by(AgentNodeProfile.node_id)
                )
            }
            ranked_observations = select(
                Observation.id.label("id"),
                func.row_number()
                .over(
                    partition_by=Observation.node_id,
                    order_by=(
                        Observation.observed_at.desc(),
                        Observation.id.desc(),
                    ),
                )
                .label("position"),
            ).where(
                Observation.kind == "health",
                Observation.node_id.in_(fleet_node_ids),
            ).subquery()
            observations = list(
                session.scalars(
                    select(Observation)
                    .join(
                        ranked_observations,
                        Observation.id == ranked_observations.c.id,
                    )
                    .where(ranked_observations.c.position == 1)
                    .order_by(Observation.node_id)
                )
            )
            ranked_inventory = select(
                NodeInventorySnapshot.id.label("id"),
                func.row_number()
                .over(
                    partition_by=NodeInventorySnapshot.node_id,
                    order_by=(
                        NodeInventorySnapshot.observed_at.desc(),
                        NodeInventorySnapshot.id.desc(),
                    ),
                )
                .label("position"),
            ).subquery()
            inventory_rows = list(
                session.scalars(
                    select(NodeInventorySnapshot)
                    .join(
                        ranked_inventory,
                        NodeInventorySnapshot.id == ranked_inventory.c.id,
                    )
                    .where(ranked_inventory.c.position == 1)
                    .order_by(NodeInventorySnapshot.node_id)
                )
            )
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(
                        AgentCertificate.node_id.in_(fleet_node_ids),
                        AgentCertificate.state == "active",
                        AgentCertificate.revoked_at.is_(None),
                    )
                    .order_by(
                        AgentCertificate.node_id,
                        AgentCertificate.not_after.desc(),
                        AgentCertificate.generation.desc(),
                    )
                )
            )
        active_certificates = {}
        for certificate in certificates:
            active_certificates.setdefault(certificate.node_id, certificate)
        latest_inventory = {}
        for inventory in inventory_rows:
            latest_inventory.setdefault(inventory.node_id, inventory)
        latest = {}
        for observation in observations:
            latest.setdefault(observation.node_id, (observation.payload, observation.observed_at))
        nodes = []
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("dashboard clock must be timezone-aware")
        current = current.astimezone(UTC)
        for node_id in fleet_node_ids:
            profile = profiles.get(node_id)
            health, observed_at = latest.get(node_id, ({}, None))
            if observed_at is not None and observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            agent_node = agent_nodes.get(node_id)
            agent_last_seen_at = agent_node.last_seen_at if agent_node is not None else None
            if agent_last_seen_at is not None:
                agent_last_seen_at = (
                    agent_last_seen_at.replace(tzinfo=UTC)
                    if agent_last_seen_at.tzinfo is None
                    else agent_last_seen_at.astimezone(UTC)
                )
            agent_age = (
                (current - agent_last_seen_at).total_seconds()
                if agent_last_seen_at is not None
                else None
            )
            agent_online = (
                agent_node is not None
                and agent_node.state == "active"
                and agent_node.revoked_at is None
                and agent_age is not None
                and 0 <= agent_age <= self._agent_online_window_seconds
            )
            certificate = active_certificates.get(node_id)
            certificate_expires_at = None if certificate is None else certificate.not_after
            if certificate_expires_at is not None and certificate_expires_at.tzinfo is None:
                certificate_expires_at = certificate_expires_at.replace(tzinfo=UTC)
            probe_age = (
                None
                if observed_at is None
                else max(0.0, (current - observed_at).total_seconds())
            )
            inventory = latest_inventory.get(node_id)
            inventory_observed_at = (
                None if inventory is None else inventory.observed_at
            )
            if inventory_observed_at is not None:
                inventory_observed_at = (
                    inventory_observed_at.replace(tzinfo=UTC)
                    if inventory_observed_at.tzinfo is None
                    else inventory_observed_at.astimezone(UTC)
                )
            inventory_age = (
                None
                if inventory_observed_at is None
                else max(0.0, (current - inventory_observed_at).total_seconds())
            )
            nodes.append({
                "id": node_id,
                "display_name": node_id if profile is None else profile.display_name,
                "hostname": "" if profile is None else profile.hostname,
                "lifecycle": "managed" if profile is None else profile.lifecycle,
                "healthy": (
                    health.get("status") in {"healthy", "warning"}
                    if observed_at is not None and isinstance(health, Mapping)
                    else None
                ),
                "stale": (
                    probe_age is None
                    or probe_age > self._health_stale_after_seconds
                ),
                "labels": (
                    {}
                    if profile is None or not isinstance(profile.labels, Mapping)
                    else dict(profile.labels)
                ),
                "memory_available_bytes": health.get(
                    "memory_available_bytes",
                    0 if inventory is None else inventory.host_memory_free_bytes,
                ) if isinstance(health, Mapping) else 0,
                "disk_available_bytes": health.get(
                    "disk_available_bytes",
                    0 if inventory is None else inventory.disk_free_bytes,
                ) if isinstance(health, Mapping) else 0,
                "probe_age_seconds": probe_age,
                "inventory_observed_at": (
                    None
                    if inventory_observed_at is None
                    else inventory_observed_at.isoformat()
                ),
                "inventory_age_seconds": inventory_age,
                "inventory_stale": (
                    inventory_age is None
                    or inventory_age > self._inventory_stale_after_seconds
                ),
                "inventory_capabilities": (
                    [] if inventory is None else list(inventory.capabilities)
                ),
                "agent_state": agent_node.state if agent_node is not None else "unregistered",
                "last_seen_at": None if agent_last_seen_at is None else agent_last_seen_at.isoformat(),
                "last_seen_age_seconds": None if agent_age is None else max(0.0, agent_age),
                "agent_last_seen_at": None if agent_last_seen_at is None else agent_last_seen_at.isoformat(),
                "agent_online": agent_online,
                "agent_platform_version": None if agent_node is None else agent_node.platform_version,
                "agent_build_digest": None if agent_node is None else agent_node.build_digest,
                "agent_active_slot": None if agent_node is None else agent_node.active_slot,
                "agent_sha256": None if agent_node is None else agent_node.agent_sha256,
                "agent_supervisor_generation": None if agent_node is None else agent_node.supervisor_generation,
                "certificate_expires_at": None if certificate_expires_at is None else certificate_expires_at.isoformat(),
                "certificate_expiry_seconds": None if certificate_expires_at is None else max(0.0, (certificate_expires_at - current).total_seconds()),
                "compatibility": protocol_version_bucket(
                    None if agent_node is None else agent_node.protocol_version,
                    minimum=self._protocol_minimum,
                    maximum=self._protocol_maximum,
                ),
            })
        return {"authority_revision": revision, "nodes": nodes}
