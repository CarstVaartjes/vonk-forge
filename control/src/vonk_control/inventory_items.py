"""One inventory path for claims and reusable runtime evidence."""

import hashlib
from collections.abc import Callable
from datetime import datetime

from vonk_agent_protocol import UnknownOutcomeError, WaitReason, canonical_message
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult

from .inventory_items_contract import AgentInventoryItems, PreflightInventoryItem
from .models import AgentNode, NodeInventoryItemsSnapshot
from .runtime_preflight import PREFLIGHT_MAX_AGE_SECONDS


def inventory_items(node: AgentNode | None) -> AgentInventoryItems:
    if node is None or node.inventory_snapshot is None:
        return AgentInventoryItems()
    items = node.inventory_snapshot.agent_items
    if not isinstance(items, AgentInventoryItems):
        raise UnknownOutcomeError(
            "Spark inventory items are unreadable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return items


def runtime_value[T](
    node: AgentNode | None, value: Callable[[AgentRuntimeIdentity], T]
) -> T | None:
    identity = inventory_items(node).runtime_identity
    return None if identity is None else value(identity)


def record_items(node: AgentNode, items: AgentInventoryItems, now: datetime) -> None:
    digest = hashlib.sha256(
        canonical_message(
            {
                "node_id": node.node_id,
                "observed_at": now.isoformat(),
                "agent_items": items,
            }
        )
    ).hexdigest()
    if node.inventory_snapshot is None:
        node.inventory_snapshot = NodeInventoryItemsSnapshot(
            node_id=node.node_id,
            observed_at=now,
            received_at=now,
            evidence_digest=digest,
            agent_items=items,
        )
    else:
        node.inventory_snapshot.agent_items = items
        node.inventory_snapshot.observed_at = now
        node.inventory_snapshot.received_at = now
        node.inventory_snapshot.evidence_digest = digest


def record_runtime(
    node: AgentNode,
    identity: AgentRuntimeIdentity,
    fingerprint: str | None,
    hostname: str | None,
    now: datetime,
) -> None:
    items = inventory_items(node)
    record_items(
        node,
        items.model_copy(
            update={
                "runtime_identity": identity,
                "hostname": items.hostname if hostname is None else hostname,
                "preflight_fingerprint": fingerprint,
            }
        ),
        now,
    )


def record_preflight(
    node: AgentNode,
    requirements_sha256: str,
    result: RuntimePreflightResult,
    now: datetime,
) -> None:
    items = inventory_items(node)
    entries = [
        item
        for item in items.preflights
        if item.requirements_sha256 != requirements_sha256
        and item.result.observed_at >= int(now.timestamp()) - PREFLIGHT_MAX_AGE_SECONDS
    ]
    entries.append(
        PreflightInventoryItem(requirements_sha256=requirements_sha256, result=result)
    )
    record_items(node, items.model_copy(update={"preflights": entries}), now)
