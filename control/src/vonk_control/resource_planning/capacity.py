"""Resource planning: capacity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vonk_agent_protocol import (
    ResourceBlockerCode,
    ResourcePlanningCode,
)

from .reasons import _reason, _same_memory_kind
from .types import (
    ENVELOPE_EXCEEDS_CAPACITY,
    ENVELOPE_UNVERIFIED,
    CapacityPlan,
    CapacitySnapshot,
    NodeCapacityPlan,
    PlannedStopRelease,
    ResourceDemand,
    ResourceReason,
    UnknownRunMemoryResidual,
)


def _minimum_known(values: Sequence[int | None]) -> int | None:
    return (
        None if None in values else min(value for value in values if value is not None)
    )


def _resident_usage_uncertainty_detail(
    capacity: CapacitySnapshot,
    residuals: Sequence[UnknownRunMemoryResidual],
    *,
    admitted: bool,
) -> str:
    observed_at = (
        capacity.evidence_observed_at.isoformat()
        if capacity.evidence_observed_at is not None
        else "timestamp unavailable"
    )
    digest = capacity.evidence_digest or "digest unavailable"
    maximum = sum(item.maximum_bytes for item in residuals)
    if admitted:
        return (
            f"Aggregate inventory {observed_at} ({digest}) reports no per-run "
            f"resident usage for {len(residuals)} exact active claim(s); their "
            f"remaining commitment is in the range 0..{maximum} bytes. Admission "
            "applies the full upper bound."
        )
    return (
        f"Capacity is unverified by aggregate inventory {observed_at} ({digest}): "
        f"{len(residuals)} exact active run claim(s) may retain 0..{maximum} "
        "bytes, and the safe upper bound does not fit. Reconcile the exact run "
        "claims and retry against fresh inventory."
    )


def plan_capacity(
    requirements: Mapping[str, ResourceDemand],
    capacities: Sequence[CapacitySnapshot],
    planned_stops: Sequence[PlannedStopRelease] = (),
    *,
    memory_floor_bytes: int = 0,
) -> CapacityPlan:
    reasons: list[ResourceReason] = []
    by_node = {item.node_id: item for item in capacities}
    releases: dict[tuple[str, str], int] = {}
    for stop in planned_stops:
        if not stop.planned:
            continue
        if type(stop.release_bytes) is not int or stop.release_bytes < 0:
            reasons.append(
                _reason(
                    ResourcePlanningCode.STOP_RELEASE_UNKNOWN,
                    "A planned stop has no valid capacity release evidence.",
                    node_id=stop.node_id,
                )
            )
            continue
        releases[(stop.node_id, stop.memory_kind)] = (
            releases.get((stop.node_id, stop.memory_kind), 0) + stop.release_bytes
        )
    nodes: list[NodeCapacityPlan] = []
    for node_id, demand in requirements.items():
        node_reasons = list(demand.reasons)
        capacity = by_node.get(node_id)
        if capacity is None:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.CAPACITY_UNKNOWN,
                    "Capacity evidence is unavailable for the selected rank.",
                    node_id=node_id,
                )
            )
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    None,
                    None,
                    None,
                    False,
                    False,
                    tuple(node_reasons),
                )
            )
            continue
        available = capacity.available_bytes
        if capacity.components:
            parts = [
                plan_capacity(
                    {node_id: demand},
                    [part],
                    planned_stops,
                    memory_floor_bytes=memory_floor_bytes,
                ).nodes[0]
                for part in capacity.components
            ]
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    _minimum_known([part.current_free_after_bytes for part in parts]),
                    _minimum_known(
                        [part.after_stop_free_after_bytes for part in parts]
                    ),
                    _minimum_known([part.selected_free_after_bytes for part in parts]),
                    any(part.stop_required for part in parts),
                    all(part.allowed for part in parts),
                    tuple(
                        dict.fromkeys(
                            reason for part in parts for reason in part.reasons
                        )
                    ),
                    tuple(
                        dict.fromkeys(
                            component
                            for part in parts
                            for component in part.insufficient_components
                        )
                    ),
                    tuple(
                        dict.fromkeys(
                            residual
                            for part in parts
                            for residual in part.unknown_run_residuals
                        )
                    ),
                )
            )
            continue
        occupied = capacity.occupied_bytes
        reserved = capacity.reserved_bytes
        unmaterialized = capacity.unmaterialized_bytes
        unknown_residuals = capacity.unknown_run_residuals
        unknown_upper_bytes = sum(
            residual.maximum_bytes for residual in unknown_residuals
        )
        for name, value in (
            ("available", available),
            ("occupied", occupied),
            ("reserved", reserved),
            ("unmaterialized", unmaterialized),
        ):
            if type(value) is not int or value < 0:
                node_reasons.append(
                    _reason(
                        ResourceBlockerCode.CAPACITY_UNKNOWN,
                        f"Current {name} capacity evidence is missing or invalid.",
                        node_id=node_id,
                    )
                )
        if capacity.evidence_state in {"unknown", "stale"}:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.CAPACITY_UNKNOWN,
                    "Current capacity evidence is missing or stale.",
                    node_id=node_id,
                )
            )
        total_bytes = demand.total_bytes
        if (
            not isinstance(available, int)
            or not isinstance(occupied, int)
            or not isinstance(reserved, int)
            or not isinstance(unmaterialized, int)
            or total_bytes is None
            or any(reason.severity == "blocker" for reason in node_reasons)
        ):
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    None,
                    None,
                    None,
                    False,
                    False,
                    tuple(node_reasons),
                )
            )
            continue
        # Aggregate inventory has no exact resident usage for a retained run.
        # Its remaining reservation is therefore a range from zero to the full
        # peak. Admission uses the safe lower-capacity bound; it never treats a
        # starting/running state as measured bytes or as a release receipt.
        current_without_unknown = available - occupied - unmaterialized - total_bytes
        current = current_without_unknown - unknown_upper_bytes
        budget_after = available - reserved - total_bytes - memory_floor_bytes
        release = releases.get((node_id, capacity.memory_kind), 0)
        if not release:
            release = max(
                (
                    value
                    for (candidate, kind), value in releases.items()
                    if candidate == node_id
                    and _same_memory_kind(kind, capacity.memory_kind)
                ),
                default=0,
            )
        idle = reserved == 0 and unmaterialized == 0 and not unknown_residuals
        declared_shortfall = (
            available - total_bytes - memory_floor_bytes < 0
            or current < memory_floor_bytes
            or budget_after < 0
        )
        if (
            idle
            and not release
            and declared_shortfall
            and available - occupied >= memory_floor_bytes
        ):
            # No Vonk claim holds memory on this Spark, so the only thing the
            # declared envelope can displace is nothing of ours. The envelope is
            # an estimate and hardware is the truth: admit the attempt, typed as
            # an unverified fit, and let the run's real outcome be the evidence.
            node_reasons.append(
                _reason(
                    ENVELOPE_UNVERIFIED,
                    f"The recipe's declared memory envelope ({total_bytes} bytes peak plus the "
                    f"{memory_floor_bytes}-byte platform floor) does not fit this Spark's "
                    f"{available - occupied} bytes of free memory, but no Vonk workload or claim "
                    "holds memory here. The declared envelope is an estimate, so the attempt is "
                    "admitted as an unverified fit; the run's own outcome is the evidence.",
                    severity="warning",
                    node_id=node_id,
                )
            )
            if available - total_bytes - memory_floor_bytes < 0:
                node_reasons.append(
                    _reason(
                        ENVELOPE_EXCEEDS_CAPACITY,
                        f"The declared envelope ({total_bytes + memory_floor_bytes} bytes) "
                        f"exceeds this Spark's {available}-byte memory capacity by "
                        f"{total_bytes + memory_floor_bytes - available} bytes. Informational: "
                        "the declared envelope is an estimate.",
                        severity="warning",
                        node_id=node_id,
                    )
                )
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    current,
                    current,
                    current,
                    False,
                    not any(reason.severity == "blocker" for reason in node_reasons),
                    tuple(node_reasons),
                )
            )
            continue
        after_stop = current + release
        budget_after_stop = budget_after + release
        current_fit = current >= memory_floor_bytes and budget_after >= 0
        after_fit = after_stop >= memory_floor_bytes and budget_after_stop >= 0
        selected = after_stop if release else current
        allowed = (after_fit if release else current_fit) and not any(
            reason.severity == "blocker" for reason in node_reasons
        )
        if budget_after < 0 and (not release or budget_after_stop < 0):
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.INSUFFICIENT_RESERVATION_BUDGET,
                    f"Exact memory commitments plus demand and reserve exceed the physical pool by {-budget_after} bytes.",
                    node_id=node_id,
                )
            )
        if (
            current_without_unknown >= memory_floor_bytes
            and current < memory_floor_bytes
            and not release
        ):
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN,
                    _resident_usage_uncertainty_detail(
                        capacity, unknown_residuals, admitted=False
                    ),
                    node_id=node_id,
                )
            )
        elif current_without_unknown < memory_floor_bytes and not release:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.INSUFFICIENT_CAPACITY,
                    f"Observed free capacity less definite claims and selected demand leaves "
                    f"{current_without_unknown} bytes before the required "
                    f"{memory_floor_bytes}-byte reserve, even if retained runs use zero bytes."
                    + (
                        " No Vonk claim holds memory on this Spark, so the shortfall is "
                        "memory used outside Vonk's workloads (the operating system "
                        "or other processes); stopping workloads cannot free it."
                        if reserved == 0
                        and unmaterialized == 0
                        and not unknown_residuals
                        else ""
                    ),
                    node_id=node_id,
                )
            )
        elif release and after_stop < memory_floor_bytes:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.INSUFFICIENT_CAPACITY_AFTER_STOP,
                    f"Selected demand leaves {selected} bytes after planned stops; {memory_floor_bytes} bytes must remain reserved.",
                    node_id=node_id,
                )
            )
        elif (
            not release
            and current >= memory_floor_bytes
            and budget_after >= 0
            and unknown_residuals
        ):
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN,
                    _resident_usage_uncertainty_detail(
                        capacity, unknown_residuals, admitted=True
                    ),
                    severity="warning",
                    node_id=node_id,
                )
            )
        nodes.append(
            NodeCapacityPlan(
                node_id,
                demand.total_bytes,
                None if unknown_residuals else current,
                None if unknown_residuals else after_stop,
                None if unknown_residuals else selected,
                not current_fit and after_fit and release > 0,
                allowed,
                tuple(node_reasons),
                (capacity.memory_kind,) if not current_fit else (),
                unknown_residuals,
            )
        )
    reasons.extend(reason for node in nodes for reason in node.reasons)
    return CapacityPlan(
        tuple(nodes),
        bool(nodes)
        and all(node.allowed for node in nodes)
        and not any(reason.severity == "blocker" for reason in reasons),
        any(node.stop_required for node in nodes),
        tuple(reasons),
    )
