"""Agent upgrades: intent."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from vonk_agent_protocol import (
    InvalidRequestReason,
    canonical_message,
)
from vonk_agent_protocol.package_source import AgentPackageSource

from ..agent_upgrade_contract import (
    AgentUpgradePackage,
    AgentUpgradeRepairManifest,
    AgentUpgradeRequestIntent,
    AgentUpgradeRolloutPayload,
)
from ..strict_json import read_stored_model
from .errors import AgentUpgradeInvalid


def _request_intent(
    value: object,
    node_ids: Sequence[str] | None,
) -> AgentUpgradeRequestIntent:
    """The requested scope as its contract: the typed value, a stored document,
    or (absent) the scope the caller's explicit Sparks imply."""

    if isinstance(value, AgentUpgradeRequestIntent):
        return value
    document = (
        {
            "all": node_ids is None,
            "selectors": None if node_ids is None else list(node_ids),
        }
        if value is None
        else value
    )
    try:
        return read_stored_model(AgentUpgradeRequestIntent, document)
    except (TypeError, ValueError):
        raise AgentUpgradeInvalid(
            "agent upgrade request intent is invalid",
            reason=InvalidRequestReason.MALFORMED,
        ) from None


def _rollout_document(payload: AgentUpgradeRolloutPayload) -> dict[str, object]:
    """The stored rollout payload; a rollout without a repair carries no key."""

    document = payload.model_dump(mode="json")
    if payload.repair_manifest is None:
        del document["repair_manifest"]
    return document


def _plan_digest(
    *,
    sources: Mapping[str, AgentPackageSource],
    authority_revision: str,
    node_ids: Sequence[str],
    package: AgentUpgradePackage,
    request_intent: AgentUpgradeRequestIntent,
    repair_manifest: AgentUpgradeRepairManifest | None,
) -> str:
    """The digest that binds a rollout plan; the same bytes at plan and resume."""

    document = {
        "sources": {
            node_id: source.model_dump(mode="json")
            for node_id, source in sources.items()
        },
        "authority_revision": authority_revision,
        "node_ids": list(node_ids),
        "package": package.model_dump(mode="json"),
        "request_intent": request_intent.model_dump(mode="json"),
        **(
            {"repair_manifest": repair_manifest.model_dump(mode="json")}
            if repair_manifest is not None
            else {}
        ),
    }
    return hashlib.sha256(canonical_message(document)).hexdigest()
