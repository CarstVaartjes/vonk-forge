"""Resource planning: preflight."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .capacity import plan_capacity
from .demand import resource_demand
from .settings import resolve_effective_settings
from .types import (
    CapacitySnapshot,
    PlannedStopRelease,
    ResourceEvidence,
    ResourcePreflightPlan,
)


def plan_resource_preflight(
    effective_context: Mapping[str, object] | object,
    evidence_by_node: Mapping[str, ResourceEvidence],
    capacities: Sequence[CapacitySnapshot],
    planned_stops: Sequence[PlannedStopRelease] = (),
    *,
    memory_floor_bytes: int = 0,
) -> ResourcePreflightPlan:
    resolution = resolve_effective_settings(effective_context)
    if resolution.settings is None:
        return ResourcePreflightPlan(None, {}, None, resolution.reasons)
    demands = {
        node_id: resource_demand(resolution.settings, evidence, node_id=node_id)
        for node_id, evidence in evidence_by_node.items()
    }
    capacity = plan_capacity(
        demands, capacities, planned_stops, memory_floor_bytes=memory_floor_bytes
    )
    return ResourcePreflightPlan(
        resolution.settings, demands, capacity, (*resolution.reasons, *capacity.reasons)
    )
