"""Read-only projection joining Git authority with operational observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .metrics import protocol_version_bucket
from .models import (
    AgentCertificate,
    AgentNode,
    NodeInventorySnapshot,
    Observation,
    PackageCandidate,
    PackageRollout,
    PackageValidationRun,
    Reconciliation,
)

_PACKAGE_CANDIDATE_STATES = frozenset({
    "discovered", "resolving", "resolved", "unsupported", "quarantined", "rejected",
})
_PACKAGE_VALIDATION_STATES = frozenset({
    "planned", "running", "passed", "failed", "retryable", "rejected", "cancelled",
})
_PACKAGE_ROLLOUT_STATES = frozenset({
    "planned", "preparing", "activating", "health-checking", "soaking", "paused",
    "rolling-back", "completed", "failed", "rolled-back", "cancelled", "waiting-for-operator",
})
_PACKAGE_ALERTS = {
    "canary-failed": "canary-failure",
    "canary-failure": "canary-failure",
    "trust_or_provenance_failure": "trust-failure",
    "trust-failure": "trust-failure",
    "no-compatible-nodes": "capacity-rejected",
    "capacity-rejected": "capacity-rejected",
    "rollback-failed": "rollback-failure",
    "rollback-failure": "rollback-failure",
}


class DashboardService:
    def __init__(
        self,
        repository,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        protocol_minimum: int = 1,
        protocol_maximum: int = 1,
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
        self._repository = repository
        self._sessions = sessions
        self._clock = clock
        self._protocol_minimum = protocol_minimum
        self._protocol_maximum = protocol_maximum
        self._agent_online_window_seconds = agent_online_window_seconds
        self._health_stale_after_seconds = health_stale_after_seconds
        self._inventory_stale_after_seconds = inventory_stale_after_seconds

    def fleet(self) -> dict[str, object]:
        commit = self._repository.head()
        document = self._repository.read_document(commit, "inventory/fleet.toml")
        parsed = document.parsed
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("nodes"), Mapping):
            raise TypeError("fleet document does not contain a node table")
        with self._sessions() as session:
            observations = list(session.scalars(select(Observation).where(Observation.kind == "health").order_by(Observation.observed_at.desc())))
            reconciliations = list(session.scalars(select(Reconciliation).where(Reconciliation.status == "succeeded").order_by(Reconciliation.created_at.desc()).limit(1)))
            agent_nodes = {
                node.node_id: node
                for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
            }
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
        active_profiles = {}
        if reconciliations and isinstance(reconciliations[0].summary, Mapping):
            raw = reconciliations[0].summary.get("node_profiles", {})
            if isinstance(raw, Mapping): active_profiles = raw
        nodes = []
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("dashboard clock must be timezone-aware")
        current = current.astimezone(UTC)
        for node_id, raw in sorted(parsed["nodes"].items()):
            if not isinstance(node_id, str) or not isinstance(raw, Mapping):
                continue
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
                "display_name": str(raw.get("display_name", node_id)),
                "hostname": str(raw.get("hostname", "")),
                "lifecycle": str(raw.get("lifecycle", "unknown")),
                "healthy": (
                    health.get("status") in {"healthy", "warning"}
                    if observed_at is not None and isinstance(health, Mapping)
                    else None
                ),
                "stale": (
                    probe_age is None
                    or probe_age > self._health_stale_after_seconds
                ),
                "labels": dict(raw.get("labels", {})) if isinstance(raw.get("labels"), Mapping) else {},
                "profile": active_profiles.get(node_id),
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
                "agent_implementation": None if agent_node is None else agent_node.agent_implementation,
                "agent_migration_state": None if agent_node is None else agent_node.migration_state,
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
        return {"commit": commit, "nodes": nodes}

    def package_summary(self) -> dict[str, dict[str, int]]:
        """Return content-free package counts and operator alert buckets.

        Package family, release, source, node, and evidence identifiers are
        intentionally absent: this is a fleet-wide dashboard projection, not
        a substitute for the authenticated package detail API.
        """
        with self._sessions() as session:
            candidates = list(session.execute(select(PackageCandidate.state)))
            validations = list(
                session.execute(
                    select(PackageValidationRun.state, PackageValidationRun.reason_code)
                )
            )
            rollouts = list(
                session.execute(select(PackageRollout.state, PackageRollout.progress))
            )
        candidate_counts: dict[str, int] = {}
        validation_counts: dict[str, int] = {}
        rollout_counts: dict[str, int] = {}
        alerts: dict[str, int] = {}

        def increment(counts: dict[str, int], value: str) -> None:
            counts[value] = counts.get(value, 0) + 1

        def alert(value: object) -> None:
            if isinstance(value, str) and value in _PACKAGE_ALERTS:
                increment(alerts, _PACKAGE_ALERTS[value])

        for (state,) in candidates:
            safe_state = state if state in _PACKAGE_CANDIDATE_STATES else "other"
            increment(candidate_counts, safe_state)
            if safe_state in {"discovered", "resolving"}:
                increment(alerts, "stuck-acquisition")
        for state, reason in validations:
            increment(
                validation_counts,
                state if state in _PACKAGE_VALIDATION_STATES else "other",
            )
            alert(reason)
        for state, progress in rollouts:
            increment(rollout_counts, state if state in _PACKAGE_ROLLOUT_STATES else "other")
            detail = progress if isinstance(progress, Mapping) else {}
            alert(detail.get("reason_code"))
            if detail.get("phase") == "acquisition" and state in {"planned", "preparing"}:
                increment(alerts, "stuck-acquisition")
        return {
            "candidates": candidate_counts,
            "validations": validation_counts,
            "rollouts": rollout_counts,
            "alerts": alerts,
        }
