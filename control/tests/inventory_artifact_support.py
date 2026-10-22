"""Fixtures write and inspect the same typed inventory items as transfer receipts."""

from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.inventory_items import inventory_items, record_artifact
from vonk_control.inventory_items_contract import InventoryArtifactItem
from vonk_control.models import AgentNode


def seed_inventory_artifact(session, *, node_id, **fields):
    node = session.get(AgentNode, node_id)
    assert node is not None
    record_artifact(
        node, InventoryArtifactItem.model_validate_json(canonical_message(fields))
    )


def clear_inventory_artifacts(session):
    for node in session.scalars(select(AgentNode)):
        if node.inventory_snapshot is not None:
            node.inventory_snapshot.agent_items = inventory_items(node).model_copy(
                update={"artifacts": []}
            )


def all_inventory_artifacts(session):
    return tuple(
        item
        for node in session.scalars(select(AgentNode))
        for item in inventory_items(node).artifacts
    )


def replace_inventory_artifact(session, node_id, artifact):
    node = session.get(AgentNode, node_id)
    assert node is not None
    record_artifact(node, artifact)
