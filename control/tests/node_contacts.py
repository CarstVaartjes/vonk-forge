"""Test access to a Spark's contact row, created on first use like enrollment does."""

from __future__ import annotations

from vonk_control.models import AgentNode, AgentNodeContact


def contact_row(node: AgentNode) -> AgentNodeContact:
    if node.contact is None:
        node.contact = AgentNodeContact(node_id=node.node_id)
    return node.contact
