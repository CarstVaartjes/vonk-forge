"""Agent upgrades: eligibility."""

from __future__ import annotations

from datetime import UTC, datetime

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.state_machines import NodeIdentityState

from ..agent_contacts import last_seen as contact_last_seen
from ..inventory.items import runtime_value
from ..models.fleet import AgentNode
from ..strict_json import read_stored_model
from .constants import _ONLINE_WINDOW
from .contract import AgentUpgradePackage
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

    @staticmethod
    def _permanent_ineligible_reason(
        node: AgentNode, package: AgentUpgradePackage
    ) -> str | None:
        """A reason this package can never be dispatched to ``node``."""

        if node.state != NodeIdentityState.ACTIVE or node.revoked_at is not None:
            return "is not active"
        if (
            runtime_value(node, lambda identity: identity.architecture)
            != package.architecture
        ):
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
        last_seen = contact_last_seen(node)
        if last_seen is None:
            return "has never reported online"
        seen = (
            last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=UTC)
        )
        if current - seen > _ONLINE_WINDOW:
            return "is not currently online"
        return None

    @staticmethod
    def _at_target(node: AgentNode, package: AgentUpgradePackage) -> bool:
        return bool(
            runtime_value(node, lambda identity: identity.build_digest)
            == package.target_build_digest
            and runtime_value(node, lambda identity: identity.binary_digest)
            == package.target_binary_digest
        )
