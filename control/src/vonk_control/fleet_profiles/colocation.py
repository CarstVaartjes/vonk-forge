"""Starts of one load that can never run beside each other on a Spark."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from vonk_agent_protocol import DesiredAssignmentState, RunSwitchCode
from vonk_agent_protocol.agent_words import ProfileAction, ProfileReasonSeverity

from ..fleet_profile_contract import (
    FleetProfileAssignmentAssessment,
    FleetProfileAssignmentPreview,
    FleetProfileReason,
)


def colocated_memory_overcommit(
    assignments: Sequence[FleetProfileAssignmentPreview],
    assessments: Sequence[FleetProfileAssignmentAssessment],
    *,
    node_ids: Collection[str] | None = None,
) -> list[FleetProfileReason]:
    """The shared-pool Sparks whose starts in this load can never fit together.

    Each start is fitted against its Spark as it is now, one at a time, so two
    starts that each fit an empty Spark pass review together. They both stay
    (the load keeps what it started), so their demands plus the platform floor
    must fit the Spark's physical pool, or the last one waits for ever on memory
    its own sibling holds. Workloads the load keeps running are already in the
    Spark's observed free memory and are not added.
    """

    starts = {
        item.assignment_id: item.recipe_title
        for item in assignments
        if item.desired_state == DesiredAssignmentState.RUNNING
        and ProfileAction.SWITCH.value in item.actions
    }
    demands: dict[str, list[tuple[str, int]]] = {}
    pools: dict[str, int] = {}
    floors: dict[str, int] = {}
    for item in assessments:
        title = starts.get(item.assignment_id)
        if title is None:
            continue
        fit = item.assessment.fit_after_stop or item.assessment.fit_current
        for node in fit.nodes:
            if (
                (node_ids is not None and node.node_id not in node_ids)
                or node.memory_pool != "shared"
                or node.memory_required_bytes is None
                or node.memory_capacity_bytes is None
                or node.memory_floor_bytes is None
            ):
                continue
            demands.setdefault(node.node_id, []).append(
                (title, node.memory_required_bytes)
            )
            pools[node.node_id] = min(
                pools.get(node.node_id, node.memory_capacity_bytes),
                node.memory_capacity_bytes,
            )
            floors[node.node_id] = max(
                floors.get(node.node_id, 0), node.memory_floor_bytes
            )
    reasons: list[FleetProfileReason] = []
    for node_id, items in sorted(demands.items()):
        needed = sum(demand for _, demand in items) + floors[node_id]
        if len(items) < 2 or needed <= pools[node_id]:
            continue
        named = ", ".join(
            f"{title} ({demand} bytes)"
            for title, demand in sorted(items, key=lambda value: -value[1])
        )
        reasons.append(
            FleetProfileReason(
                code=RunSwitchCode.RESOURCE_DEMAND_EXCEEDS_CAPACITY,
                detail=(
                    f"This load starts {named} together on Spark {node_id}: with "
                    f"the {floors[node_id]}-byte platform floor they need "
                    f"{needed} bytes of its {pools[node_id]}-byte memory pool, "
                    "so they can never all run there."
                )[:512],
                severity=ProfileReasonSeverity.ERROR.value,
            )
        )
    return reasons
