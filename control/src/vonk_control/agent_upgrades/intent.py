"""Agent upgrades: intent."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.package_source import AgentPackageSource
from vonk_agent_protocol.wire_model import canonical_message

from ..strict_json import read_stored_model
from .contract import AgentUpgradePackage, AgentUpgradeRequestIntent
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


def _plan_digest(
    *,
    sources: Mapping[str, AgentPackageSource],
    authority_revision: str,
    node_ids: Sequence[str],
    package: AgentUpgradePackage,
    request_intent: AgentUpgradeRequestIntent,
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
    }
    return hashlib.sha256(canonical_message(document)).hexdigest()
