"""Clock policy follows the selected immutable application, including idle Sparks."""

from collections.abc import Sequence

from sqlalchemy.orm import Session
from vonk_agent_protocol.gpu_clock import (
    GpuClockIntent,
    GpuClockRange,
    default_gpu_clock,
)

from .fleet_profile_contract.definitions import StoredFleetProfileAssignment
from .fleet_profile_contract.effects import FleetProfileGpuClockEffect
from .fleet_profiles.service import FleetProfileService
from .lifecycle.evidence import Residue
from .models import FleetProfileApplication, FleetProfileSelection


def selected_gpu_clock(session: Session, node_id: str) -> GpuClockIntent:
    return selected_gpu_clocks(session, (node_id,))[node_id]


def selected_gpu_clocks(
    session: Session, node_ids: Sequence[str]
) -> dict[str, GpuClockIntent]:
    selection = session.get(FleetProfileSelection, 1)
    if selection is None:
        return {
            node_id: GpuClockIntent(generation=0, range=default_gpu_clock())
            for node_id in node_ids
        }
    application = session.get(FleetProfileApplication, selection.application_id)
    if application is None or application.selection_generation != selection.generation:
        raise ValueError("selected profile GPU clock intent is unavailable")
    intended = FleetProfileService._intended_profile(application, session=session)
    if isinstance(
        intended, Residue
    ):  # unknown stored evidence, not a Python argument type
        raise ValueError("selected profile GPU clock snapshot is unavailable")  # noqa: TRY004
    ranges = {}
    for assignment in intended.assignments:
        requested = assignment.gpu_clock or default_gpu_clock()
        for node in assignment.nodes:
            if node.node_id in ranges and ranges[node.node_id] != requested:
                raise ValueError(
                    "selected profile contains conflicting GPU clock ranges"
                )
            ranges[node.node_id] = requested
    return {
        node_id: GpuClockIntent(
            generation=selection.generation,
            range=ranges.get(node_id, default_gpu_clock()),
        )
        for node_id in node_ids
    }


def profile_gpu_clock_effects(
    session: Session,
    node_ids: Sequence[str],
    assignments: Sequence[StoredFleetProfileAssignment],
) -> list[FleetProfileGpuClockEffect]:
    """Bind changes of desired policy, independent of volatile driver evidence."""
    try:
        previous = selected_gpu_clocks(session, node_ids)
    except ValueError:
        previous = {}
    desired = {node_id: default_gpu_clock() for node_id in node_ids}
    assigned: dict[str, GpuClockRange] = {}
    for assignment in assignments:
        requested = assignment.gpu_clock or default_gpu_clock()
        for node in assignment.nodes:
            if node.node_id in assigned and assigned[node.node_id] != requested:
                raise ValueError("profile contains conflicting GPU clock ranges")
            assigned[node.node_id] = requested
            if node.node_id in desired:
                desired[node.node_id] = requested
    return [
        FleetProfileGpuClockEffect(
            node_id=node_id,
            previous_desired=previous[node_id].range if node_id in previous else None,
            desired=desired[node_id],
        )
        for node_id in sorted(node_ids)
        if node_id not in previous or previous[node_id].range != desired[node_id]
    ]


def gpu_clock_start_intent(session: Session, node_id: str) -> GpuClockIntent:
    """Every start binds the selected policy; GB10 need not enumerate levels."""
    return selected_gpu_clock(session, node_id)
