"""Shared strict validators for the public FleetSnapshot readiness contract."""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection
from typing import Any

NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")


def is_ready_node(
    node: dict[str, Any] | None,
    *,
    required_capabilities: Collection[str] = (),
) -> bool:
    """Return whether a serialized FleetSnapshot node is operationally ready."""

    connection = node.get("connection") if isinstance(node, dict) else None
    inventory = node.get("inventory") if isinstance(node, dict) else None
    capabilities = (
        inventory.get("capabilities") if isinstance(inventory, dict) else None
    )
    return bool(
        isinstance(node, dict)
        and node.get("lifecycle") == "ready"
        and isinstance(connection, dict)
        and connection.get("agent_state") == "active"
        and connection.get("certificate_state") == "valid"
        and connection.get("online_state") == "online"
        and isinstance(inventory, dict)
        and inventory.get("freshness") == "fresh"
        and isinstance(capabilities, list)
        and all(
            isinstance(capability, str) and capability in capabilities
            for capability in required_capabilities
        )
    )


def active_agents(payload: object) -> dict[str, dict[str, Any]]:
    """Validate and index the live agent-list serialization by node id."""

    agents = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(agents, list):
        raise TypeError("agent response is invalid")
    indexed: dict[str, dict[str, Any]] = {}
    for agent in agents:
        node_id = agent.get("node_id") if isinstance(agent, dict) else None
        generation = (
            agent.get("supervisor_generation") if isinstance(agent, dict) else None
        )
        if (
            not isinstance(agent, dict)
            or not isinstance(node_id, str)
            or NODE_ID.fullmatch(node_id) is None
            or node_id in indexed
            or agent.get("state") != "active"
            or agent.get("migration_state") != "complete"
            or agent.get("stale") is not False
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ValueError("agent response is invalid")
        indexed[node_id] = agent
    return indexed


def restart_identity(
    node: dict[str, Any] | None,
    agent: dict[str, Any] | None,
    *,
    required_capabilities: Collection[str] = (),
) -> dict[str, object]:
    """Extract the independently advancing host/supervisor restart identity."""

    if not is_ready_node(node, required_capabilities=required_capabilities):
        raise ValueError("fleet node is not ready")
    if not isinstance(agent, dict):
        raise TypeError("agent response is invalid")
    generation = agent.get("supervisor_generation")
    if (
        agent.get("node_id") != node.get("id")
        or agent.get("state") != "active"
        or agent.get("migration_state") != "complete"
        or agent.get("stale") is not False
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ValueError("agent response is invalid")
    telemetry = node.get("telemetry")
    sample = telemetry.get("sample") if isinstance(telemetry, dict) else None
    boot_id = sample.get("boot_id") if isinstance(sample, dict) else None
    try:
        parsed_boot_id = uuid.UUID(boot_id) if isinstance(boot_id, str) else None
    except ValueError as exc:
        raise ValueError("fleet boot identity is invalid") from exc
    if parsed_boot_id is None or str(parsed_boot_id) != boot_id:
        raise ValueError("fleet boot identity is invalid")
    return {"boot_id": boot_id, "supervisor_generation": generation}
