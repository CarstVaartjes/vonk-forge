"""Agent upgrades: eligibility."""

from __future__ import annotations

from datetime import UTC, datetime

from vonk_agent_protocol import (
    InvalidRequestReason,
)

from ..agent_upgrade_contract import (
    AgentUpgradePackage,
    AgentUpgradeRepairManifest,
)
from ..models import AgentNode
from ..strict_json import read_stored_model
from .constants import _ONLINE_WINDOW
from .errors import AgentUpgradeInvalid


class EligibilityMixin:
    @staticmethod
    def _package(value: object) -> AgentUpgradePackage:
        """The one ingress of a package descriptor (the release channel)."""

        try:
            return read_stored_model(AgentUpgradePackage, value)
        except (TypeError, ValueError):
            raise AgentUpgradeInvalid(
                "agent upgrade package is invalid",
                reason=InvalidRequestReason.MALFORMED,
            ) from None

    @classmethod
    def _repair_manifest(
        cls,
        manifest: AgentUpgradeRepairManifest,
        package: AgentUpgradePackage,
    ) -> AgentUpgradeRepairManifest:
        expected_url = (
            "https://install.vonkforge.ai/repair-capsules/"
            f"{manifest.node_id}/{manifest.authority_sha256}/"
            f"{manifest.package.package_sha256}/vonk-forge-agent.deb"
        )
        if manifest.package.package_url != expected_url:
            raise AgentUpgradeInvalid(
                "agent repair package URL is not canonical",
                reason=InvalidRequestReason.MALFORMED,
            )
        if package != manifest.package:
            raise AgentUpgradeInvalid(
                "agent repair manifest does not match its package descriptor",
                reason=InvalidRequestReason.CONFLICT,
            )
        return manifest

    @staticmethod
    def _permanent_ineligible_reason(
        node: AgentNode, package: AgentUpgradePackage
    ) -> str | None:
        """A reason this package can never be dispatched to ``node``."""

        if node.state != "active" or node.revoked_at is not None:
            return "is not active"
        if node.architecture != package.architecture:
            return "has an incompatible architecture"
        return None

    @classmethod
    def _ineligible_reason(
        cls,
        node: AgentNode,
        package: AgentUpgradePackage,
        now: datetime,
    ) -> str | None:
        reason = cls._permanent_ineligible_reason(node, package)
        if reason is not None:
            return reason
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        last_seen = node.last_seen_at
        if last_seen is None:
            return "has never reported online"
        seen = (
            last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=UTC)
        )
        if seen > current or current - seen > _ONLINE_WINDOW:
            return "is not currently online"
        return None

    @staticmethod
    def _at_target(node: AgentNode, package: AgentUpgradePackage) -> bool:
        return bool(
            node.build_digest == package.target_build_digest
            and node.binary_digest == package.target_binary_digest
        )
