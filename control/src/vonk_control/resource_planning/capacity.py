"""Resource planning: capacity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from vonk_agent_protocol import (
    ResourceBlockerCode,
    ResourcePlanningCode,
)
from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.swap import (
    GIB,
    SWAP_POLICY,
    swap_needed_bytes,
    swap_reaches,
    usable_swap_bytes,
)
from vonk_forge_contracts.memory_admission import evaluate_memory_admission

from ..operation_blockers import SwapShortfallEvidence
from .reasons import _reason, _same_memory_kind
from .types import (
    CapacityPlan,
    CapacitySnapshot,
    NodeCapacityPlan,
    PlannedStopRelease,
    ResourceDemand,
    ResourceReason,
    SwapCapacity,
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


#: How many charged claims a capacity reason names.
_NAMED_CLAIMS = 3


def _charged_claims_detail(capacity: CapacitySnapshot, shortfall_bytes: int) -> str:
    """Name the not-yet-materialized claims a shortfall is charged against.

    A shortfall these claims cover is a wait on their owners, not a demand
    that cannot fit; the detail says which.
    """
    holders = capacity.unmaterialized_holders
    if not holders:
        return ""
    # A reason's detail is bounded: name the largest claims first.
    largest = sorted(holders, key=lambda holder: -holder.amount_bytes)
    named = ", ".join(
        f"{holder.owner.kind.value} {holder.owner.id} ({holder.amount_bytes} bytes)"
        for holder in largest[:_NAMED_CLAIMS]
    ) + (
        f" and {len(largest) - _NAMED_CLAIMS} more"
        if len(largest) > _NAMED_CLAIMS
        else ""
    )
    # What the snapshot charges for them (a promise replacing a live claim is
    # charged only its increment), not their declared sum.
    if (capacity.unmaterialized_bytes or 0) >= shortfall_bytes:
        return f" The demand fits once these claims are released: {named}."
    return f" Charged claims: {named}."


def _holds_nothing(capacity: CapacitySnapshot, *, stopping: bool) -> bool:
    """Whether no Vonk claim, kept run, releasing run or planned stop is here."""
    return (
        not capacity.reserved_bytes
        and not capacity.unmaterialized_bytes
        and not capacity.unknown_run_residuals
        and not capacity.kept_bytes
        and not capacity.releasing_bytes
        and not stopping
    )


def _above_idle_maximum(
    capacity: CapacitySnapshot,
    demand_bytes: int,
    *,
    memory_floor_bytes: int,
    stopping: bool,
) -> str | None:
    """Why an idle Spark admits less than this demand, if so.

    The demand is a recipe's declared peak, an estimate until a try measures
    it. It is above the idle maximum when the demand and the floor exceed the
    physical pool, or when no Vonk claim, kept run or planned stop is left on
    the Spark and even the most memory it reported free recently (the host's
    own use only) falls short. The estimate is then tried as the only claim on
    an empty Spark; a shortfall while something is held stays a wait.
    """
    pool = capacity.available_bytes
    if pool is None:
        return None
    if not evaluate_memory_admission(
        demand_bytes, pool, memory_floor_bytes=memory_floor_bytes
    ).admissible:
        return (
            f"The estimated demand of {demand_bytes} bytes plus the "
            f"{memory_floor_bytes}-byte platform floor exceeds this Spark's "
            f"{pool}-byte memory pool; an idle Spark admits less"
        )
    best = evaluate_memory_admission(
        demand_bytes,
        pool,
        occupied_bytes=max(
            0, (capacity.occupied_bytes or 0) - capacity.free_upside_bytes
        ),
        memory_floor_bytes=memory_floor_bytes,
    )
    if _holds_nothing(capacity, stopping=stopping) and not best.admissible:
        return (
            f"The host itself uses {capacity.occupied_bytes} of this Spark's "
            f"{pool}-byte pool, and the estimated demand of {demand_bytes} bytes "
            f"leaves {best.free_after_bytes} bytes before the required "
            f"{memory_floor_bytes}-byte floor even at the most free memory the "
            "Spark reported recently; an idle Spark admits less"
        )
    return None


def _tried_swap_need(demand: ResourceDemand) -> tuple[int, int, bool]:
    """The load rise and minimum swap a try needs, and whether they were cut.

    A declared rise and a declared minimum swap are estimates: a try needs at
    most the largest swap setting, and the host memory guard stops it if swap
    really runs out.
    """

    largest_setting = SWAP_POLICY.maximum_gib * GIB
    rise = min(demand.load_overshoot_bytes, usable_swap_bytes(largest_setting))
    minimum = min(demand.minimum_swap_bytes, largest_setting)
    clamped = rise < demand.load_overshoot_bytes or minimum < demand.minimum_swap_bytes
    return rise, minimum, clamped


def _swap_carries(demand: ResourceDemand, swap: SwapCapacity, *, in_use: bool) -> bool:
    """Whether the Spark's active swap carries the load a try needs.

    A rise cut to the largest setting is an estimate: it asks only that the
    swap has reached that setting (the same ``swap_reaches`` rule) and that no
    other load still holds swap, because swap in use or a page short of the
    setting would otherwise never clear. The guard decides the rest. An uncut rise must fit the usable swap beside
    what other loads hold (and, before a stop, what is in use).
    """

    rise, minimum, _clamped = _tried_swap_need(demand)
    if rise < demand.load_overshoot_bytes:
        # What other ranks still loading hold is theirs; the rest is the guard's.
        return not swap.held_bytes and swap_reaches(
            swap.total_bytes, SWAP_POLICY.maximum_gib * GIB
        )
    return rise + swap.held_bytes + (
        swap.used_bytes if in_use else 0
    ) <= usable_swap_bytes(swap.total_bytes) and swap_reaches(swap.total_bytes, minimum)


def _load_swap_reason(
    node_id: str, demand: ResourceDemand, capacity: CapacitySnapshot | None
) -> tuple[ResourceReason, ...]:
    """Whether swap can carry this load's rise above its steady demand.

    Swap left usable within the host memory guard's margins must hold the
    rise plus what other ranks still loading on this Spark hold. The rise and
    the minimum swap are estimates, cut to the largest swap setting: a
    shortfall is a wait for swap, never a refusal.
    """

    rise, minimum, clamped = _tried_swap_need(demand)
    if rise <= 0 and minimum <= 0:
        return ()
    reasons: list[ResourceReason] = []
    if clamped:
        reasons.append(
            _reason(
                ResourcePlanningCode.ESTIMATE_UNCERTAIN,
                f"The declared load rise ({demand.load_overshoot_bytes} bytes) or "
                f"minimum swap ({demand.minimum_swap_bytes} bytes) is above the "
                f"largest swap setting ({SWAP_POLICY.maximum_gib} GiB); it is "
                "tried with that and the host memory guard decides.",
                severity=ProfileReasonSeverity.WARNING.value,
                node_id=node_id,
            )
        )
    if capacity is None:
        # Unknown capacity is already the blocker.
        return tuple(reasons)
    swap = capacity.swap
    usable = usable_swap_bytes(swap.total_bytes)
    if _swap_carries(demand, swap, in_use=True):
        return tuple(reasons)
    setting = min(
        SWAP_POLICY.maximum_gib * GIB,
        max(swap_needed_bytes(rise + swap.used_bytes), -(-minimum // GIB) * GIB),
    )
    jobs = [holder.run_id for holder in swap.holders if holder.job_run]
    if swap.desired_bytes is not None and setting > swap.desired_bytes:
        remedy = f"Raise the Spark swap setting to at least {setting // GIB} GiB."
    elif jobs:
        remedy = (
            f"It starts once job run {', '.join(jobs[:_NAMED_CLAIMS])} here stops"
            + (
                " and the other loads finish loading."
                if len(jobs) < len(swap.holders)
                else "."
            )
        )
    elif swap.held_bytes:
        remedy = "It starts once the other loads here finish loading."
    elif swap.desired_bytes is not None and not swap_reaches(
        swap.total_bytes, swap.desired_bytes
    ):
        remedy = "It starts once the Spark's swap reaches its setting."
    else:
        remedy = "It starts once swap in use falls."
    setting_text = (
        "no setting known"
        if swap.desired_bytes is None
        else f"setting {swap.desired_bytes} bytes"
    )
    reasons.append(
        _reason(
            ResourceBlockerCode.INSUFFICIENT_SWAP,
            f"The load rises {rise} bytes above its steady demand and needs at "
            f"least {minimum} bytes of swap. Swap: "
            f"{swap.total_bytes} bytes active ({setting_text}), "
            f"{swap.used_bytes} in use, {swap.held_bytes} held by loads still "
            f"starting or job runs; {usable} bytes are usable within the host memory guard's "
            f"margins. {remedy}",
            node_id=node_id,
            swap=SwapShortfallEvidence(
                active_bytes=swap.total_bytes,
                desired_bytes=swap.desired_bytes,
                used_bytes=swap.used_bytes,
                held_bytes=swap.held_bytes,
                usable_bytes=usable,
                rise_bytes=rise,
                needed_setting_bytes=setting,
            ),
        )
    )
    return tuple(reasons)


def stop_can_clear_swap(demand: ResourceDemand, swap: SwapCapacity) -> bool:
    """Whether only swap in use keeps this load out, so a stop may clear it.

    ``swap`` must leave out the stopped runs' held rises (an after-stop
    view). A stop frees at most the swap in use; a rise or other loads' held
    rises beyond the usable swap, or too little active swap, stay a wait
    before the stop, and the run that would be stopped keeps serving.
    """

    return _swap_carries(demand, swap, in_use=False)


def plan_capacity(
    requirements: Mapping[str, ResourceDemand],
    capacities: Sequence[CapacitySnapshot],
    planned_stops: Sequence[PlannedStopRelease] = (),
    *,
    memory_floor_bytes: int = 0,
) -> CapacityPlan:
    """The steady demand against memory, then the load's rise against swap."""

    memory = _plan_memory_capacity(
        requirements, capacities, planned_stops, memory_floor_bytes=memory_floor_bytes
    )
    by_node = {item.node_id: item for item in capacities}
    nodes: list[NodeCapacityPlan] = []
    swap_reasons: list[ResourceReason] = []
    for node in memory.nodes:
        found = _load_swap_reason(
            node.node_id, requirements[node.node_id], by_node.get(node.node_id)
        )
        if found:
            swap_reasons.extend(found)
            node = replace(
                node,
                allowed=node.allowed
                and not any(
                    reason.severity == ProfileReasonSeverity.BLOCKER.value
                    for reason in found
                ),
                reasons=(*node.reasons, *found),
            )
        nodes.append(node)
    return CapacityPlan(
        tuple(nodes),
        memory.allowed
        and not any(
            reason.severity == ProfileReasonSeverity.BLOCKER.value
            for reason in swap_reasons
        ),
        memory.stop_before_prepare,
        (*memory.reasons, *swap_reasons),
    )


def _plan_memory_capacity(
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
                _plan_memory_capacity(
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
                    _minimum_known([part.required_bytes for part in parts]),
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
        # An estimate claims at most the Spark less the floor: a pure function
        # of the recipe and the node total, so review, start and recovery agree.
        charged = (
            total_bytes
            if available <= memory_floor_bytes
            else min(total_bytes, available - memory_floor_bytes)
        )
        current_admission = evaluate_memory_admission(
            charged,
            available,
            occupied_bytes=occupied,
            reserved_bytes=reserved,
            unmaterialized_bytes=unmaterialized,
            unknown_residual_bytes=unknown_upper_bytes,
            memory_floor_bytes=memory_floor_bytes,
        )
        after_admission = evaluate_memory_admission(
            charged,
            available,
            occupied_bytes=occupied,
            reserved_bytes=reserved,
            unmaterialized_bytes=unmaterialized,
            unknown_residual_bytes=unknown_upper_bytes,
            release_bytes=release,
            memory_floor_bytes=memory_floor_bytes,
        )
        current = current_admission.free_after_bytes
        current_without_unknown = current + unknown_upper_bytes
        budget_after = current_admission.commitment_headroom_bytes
        after_stop = after_admission.free_after_bytes
        budget_after_stop = after_admission.commitment_headroom_bytes
        current_fit = current_admission.admissible
        after_fit = after_admission.admissible
        selected = after_stop if release else current
        # A planned stop frees memory even when its release is unknown.
        stopping = any(
            stop.planned and stop.node_id == node_id for stop in planned_stops
        )
        above = _above_idle_maximum(
            capacity,
            total_bytes,
            memory_floor_bytes=memory_floor_bytes,
            stopping=stopping,
        )
        # An estimate above the idle maximum is tried as the only claim on an
        # empty Spark; with anything held, or the host using the rest, it waits.
        tried = (
            above is not None
            and _holds_nothing(capacity, stopping=stopping)
            and available - occupied - memory_floor_bytes > 0
        )
        allowed = (tried or (after_fit if release else current_fit)) and not any(
            reason.severity == ProfileReasonSeverity.BLOCKER.value
            for reason in node_reasons
        )
        if tried:
            node_reasons.append(
                _reason(
                    ResourcePlanningCode.ESTIMATE_UNCERTAIN,
                    f"{above}; it is tried as the only claim on this Spark (claim "
                    f"{charged} bytes) and the host memory guard stops it if it "
                    "runs out.",
                    severity=ProfileReasonSeverity.WARNING.value,
                    node_id=node_id,
                )
            )
        else:
            if budget_after < 0 and (not release or budget_after_stop < 0):
                node_reasons.append(
                    _reason(
                        ResourceBlockerCode.INSUFFICIENT_RESERVATION_BUDGET,
                        f"Exact memory commitments plus demand and reserve exceed the physical pool by {-budget_after} bytes."
                        + _charged_claims_detail(capacity, -budget_after),
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
                        + _charged_claims_detail(
                            capacity, memory_floor_bytes - current_without_unknown
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
                charged,
                None if unknown_residuals else current,
                None if unknown_residuals else after_stop,
                None if unknown_residuals else selected,
                not current_fit and after_fit and release > 0,
                allowed,
                tuple(node_reasons),
                (capacity.memory_kind,) if not (tried or current_fit) else (),
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
