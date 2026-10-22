from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vonk_control.agent_jobs import AgentJobService

PACKAGED_RUNTIME_IDENTITY = {
    "architecture": "linux-amd64",
    "binary_digest": "c" * 64,
    "build_digest": "sha256:" + "b" * 64,
    "semantic_version": "1.2.3",
}
_DEFAULT_IDENTITY = object()


def claim_agent(
    service: AgentJobService,
    *args: Any,
    runtime_identity: Mapping[str, object] | None | object = _DEFAULT_IDENTITY,
    **kwargs: Any,
):
    """Claim through the exact packaged Rust runtime contract."""
    if runtime_identity is _DEFAULT_IDENTITY:
        runtime_identity = PACKAGED_RUNTIME_IDENTITY
    assert runtime_identity is None or isinstance(runtime_identity, Mapping)
    return service.claim(
        *args,
        runtime_identity=runtime_identity,
        **kwargs,
    )


def inventory_items_fixture(
    node_id,
    *,
    observed_at=None,
    identity=None,
    hostname=None,
    preflight_fingerprint=None,
):
    """A typed inventory item can exist before the first capacity report."""
    import hashlib
    from datetime import UTC, datetime

    from vonk_agent_protocol import canonical_message
    from vonk_control.inventory_items_contract import AgentInventoryItems
    from vonk_control.models import NodeInventoryItemsSnapshot

    at = observed_at or datetime(2026, 8, 7, 12, tzinfo=UTC)
    items = AgentInventoryItems(
        runtime_identity=identity,
        hostname=hostname,
        preflight_fingerprint=preflight_fingerprint,
    )
    return NodeInventoryItemsSnapshot(
        node_id=node_id,
        observed_at=at,
        received_at=at,
        agent_items=items,
        evidence_digest=hashlib.sha256(
            canonical_message({"node_id": node_id, "agent_items": items})
        ).hexdigest(),
    )


def set_inventory_identity(node, **fields):
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_control.inventory_items import inventory_items

    items = inventory_items(node)
    if any(value is None for value in fields.values()):
        identity = None
    else:
        previous = items.runtime_identity
        identity = AgentRuntimeIdentity.model_validate(
            {
                **(
                    PACKAGED_RUNTIME_IDENTITY
                    if previous is None
                    else previous.model_dump()
                ),
                **fields,
            }
        )
    if node.inventory_snapshot is None:
        node.inventory_snapshot = inventory_items_fixture(
            node.node_id, identity=identity
        )
    else:
        node.inventory_snapshot.agent_items = items.model_copy(
            update={"runtime_identity": identity}
        )


def set_inventory_fingerprint(node, fingerprint):
    from vonk_control.inventory_items import inventory_items

    if node.inventory_snapshot is None:
        node.inventory_snapshot = inventory_items_fixture(node.node_id)
    node.inventory_snapshot.agent_items = inventory_items(node).model_copy(
        update={"preflight_fingerprint": fingerprint}
    )


def set_inventory_hostname(session, node_id, hostname):
    from vonk_control.inventory_items import inventory_items
    from vonk_control.models import AgentNode

    node = session.get(AgentNode, node_id)
    assert node is not None
    if node.inventory_snapshot is None:
        node.inventory_snapshot = inventory_items_fixture(
            node.node_id, hostname=hostname
        )
    else:
        node.inventory_snapshot.agent_items = inventory_items(node).model_copy(
            update={"hostname": hostname}
        )
