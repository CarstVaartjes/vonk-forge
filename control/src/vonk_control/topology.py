"""Validate an exact recipe topology against deterministic local placements."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .recipe_contract import RecipeContractError, recipe_topology


class TopologyError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Placement:
    node_id: str
    rank: int
    role: str
    endpoint_owner: bool = False


def validate_topology(
    recipe: Mapping[str, object],
    placements: Sequence[Placement],
    capabilities: Mapping[str, tuple[str, ...]],
) -> tuple[Placement, ...]:
    try:
        topology = recipe_topology(recipe)
    except RecipeContractError as error:
        raise TopologyError("topology.invalid", str(error)) from error
    ordered = tuple(sorted(placements, key=lambda item: item.rank))
    nodes = [item.node_id for item in ordered]
    ranks = [item.rank for item in ordered]
    if (
        not ordered
        or len(nodes) != len(set(nodes))
        or ranks != list(range(len(ordered)))
        or len(ordered) != topology["node_count"]
    ):
        raise TopologyError(
            "topology.placement_invalid",
            "placement must match the exact topology with unique contiguous ranks",
        )
    roles = topology.get("roles")
    if not isinstance(roles, list):
        raise TopologyError("topology.invalid", "topology roles are invalid")
    expected_placements = [
        (str(role["name"]), bool(role["endpoint_owner"]))
        for role in roles
        if isinstance(role, Mapping)
        for _ in range(int(role["count"]))
    ]
    if [(item.role, item.endpoint_owner) for item in ordered] != expected_placements:
        raise TopologyError(
            "topology.role_mismatch", "placement roles do not match the topology"
        )
    if any("runtime.vonk.v1" not in capabilities.get(node, ()) for node in nodes):
        raise TopologyError(
            "topology.runtime_capability_missing",
            "every GPU node must advertise runtime.vonk.v1",
        )
    fabric = topology.get("fabric")
    if not isinstance(fabric, Mapping):
        raise TopologyError("topology.fabric_missing", "topology fabric is missing")
    connectivity = str(fabric["connectivity"])
    required = int(fabric["minimum_bandwidth_mbps"])
    if len(ordered) == 1:
        if connectivity != "none":
            raise TopologyError(
                "topology.fabric_invalid", "single-node topology must use no fabric"
            )
        return ordered
    if connectivity == "none":
        raise TopologyError(
            "topology.fabric_invalid", "multi-node topology must declare fabric"
        )
    accepted = {
        "connected": {"connected", "full_mesh", "switch"},
        "full_mesh": {"full_mesh", "switch"},
        "switch": {"switch"},
    }.get(connectivity, set())
    for node in nodes:
        speeds = [
            speed
            for value in capabilities.get(node, ())
            for kind, speed in [_fabric_capability(value)]
            if kind in accepted
        ]
        if not speeds or max(speeds) < required:
            raise TopologyError(
                "topology.fabric_insufficient",
                f"{node} lacks {required} Mbps {connectivity} fabric",
            )
    return ordered


def _fabric_capability(value: str) -> tuple[str, int]:
    parts = value.split(".")
    if (
        len(parts) == 4
        and parts[0] == "fabric"
        and parts[2] == "mbps"
        and parts[3].isdigit()
    ):
        return parts[1], int(parts[3])
    return "", 0
