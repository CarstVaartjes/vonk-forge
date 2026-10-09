"""Agent upgrades: plan."""

from __future__ import annotations

from dataclasses import dataclass

from vonk_agent_protocol.package_source import AgentPackageSource

from ..agent_upgrade_contract import (
    AgentUpgradePackage,
    AgentUpgradeRepairManifest,
    AgentUpgradeRequestIntent,
)


@dataclass(frozen=True, slots=True)
class AgentUpgradePlan:
    authority_revision: str
    node_ids: tuple[str, ...]
    package: AgentUpgradePackage
    plan_digest: str
    repair_manifest: AgentUpgradeRepairManifest | None
    request_intent: AgentUpgradeRequestIntent
    sources: dict[str, AgentPackageSource]
    #: Selected Sparks this rollout will not touch, with the reason.  A Spark
    #: that already runs the target is a no-op, not a conflict; one that can
    #: never take this package is reported instead of refusing the fleet.
    skipped: dict[str, str]
