"""A recipe's declared peak is an estimate: it is tried, not refused.

Owner 2026-11-02: assume the creator made a working recipe, use all its
settings, measure the real usage, and do not block until it is known whether
it works. An estimate above what an idle Spark admits is tried as the only
claim on an empty Spark and waits (a typed wait, never "never fits") while
anything else holds memory there. The claim is ``min(estimate, pool - floor)``,
a pure function of the recipe and the node total, so it never moves with the
free-memory sample.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from vonk_agent_protocol import ResourceBlockerCode, ResourcePlanningCode
from vonk_agent_protocol.swap import GIB, SWAP_POLICY, usable_swap_bytes
from vonk_control.resource_planning import (
    PLATFORM_MEMORY_FLOOR_BYTES,
    MemoryReservationTotals,
    PlannedStopRelease,
    SwapCapacity,
    memory_capacity_snapshot,
    plan_capacity,
)

from .test_resource_planning import (
    _SPARK_POOL,
    _blocker_codes,
    _codes,
    _declared_demand,
    _idle_capacity,
    _tried,
)

_FLOOR = PLATFORM_MEMORY_FLOOR_BYTES
_FREE = 125_650_657_280
#: Above the most an idle Spark admits, below the pool less the floor.
_OVER_MAX = _FREE - _FLOOR + 1_000_000_000


def _plan(demand, capacity, stops=()):
    return plan_capacity(
        {"rank-0": demand}, [capacity], stops, memory_floor_bytes=_FLOOR
    )


def test_an_estimate_above_the_idle_maximum_is_tried_on_an_empty_spark() -> None:
    plan = _plan(_declared_demand(_OVER_MAX), _idle_capacity(_SPARK_POOL, _FREE))
    assert plan.allowed
    assert _blocker_codes(plan) == []
    (node,) = plan.nodes
    # The claim is the estimate; the Spark is left below its floor, as the
    # estimate asked for.
    assert node.required_bytes == _OVER_MAX
    assert node.selected_free_after_bytes == _FREE - _OVER_MAX
    assert len(_tried(plan)) == 1


def test_an_estimate_above_the_physical_pool_is_tried_with_the_pool_less_the_floor() -> (
    None
):
    plan = _plan(
        _declared_demand(_SPARK_POOL + 1_000_000_000),
        _idle_capacity(_SPARK_POOL, _FREE),
    )
    assert plan.allowed
    assert plan.nodes[0].required_bytes == _SPARK_POOL - _FLOOR


@pytest.mark.parametrize("estimate", [_OVER_MAX, _SPARK_POOL + 1_000_000_000])
def test_the_claim_does_not_depend_on_free_memory(estimate: int) -> None:
    # Review, start and recovery each re-plan against a newer sample; a claim
    # that followed the sample would make every plan stale.
    claims = {
        _plan(_declared_demand(estimate), _idle_capacity(_SPARK_POOL, free))
        .nodes[0]
        .required_bytes
        for free in (_FREE, _FREE - 1_500_000_000, _FREE - 10)
    }
    assert claims == {min(estimate, _SPARK_POOL - _FLOOR)}


def test_an_estimate_waits_while_the_spark_holds_anything() -> None:
    held = _plan(
        _declared_demand(_OVER_MAX),
        _idle_capacity(_SPARK_POOL, _FREE - 1_000_000_000, claimed=1_000_000_000),
    )
    assert not held.allowed
    assert _blocker_codes(held)
    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _codes(held)

    stopping = _plan(
        _declared_demand(_OVER_MAX),
        _idle_capacity(_SPARK_POOL, _FREE),
        [PlannedStopRelease("old", "rank-0", "unified-memory", 100_000_000, True)],
    )
    assert not stopping.allowed
    assert _blocker_codes(stopping) == [
        ResourceBlockerCode.INSUFFICIENT_CAPACITY_AFTER_STOP
    ]


def test_an_estimate_waits_when_the_host_leaves_nothing_free() -> None:
    # A real host fact: nothing above the floor is left to claim.
    plan = _plan(_declared_demand(_OVER_MAX), _idle_capacity(_SPARK_POOL, _FLOOR - 1))
    assert not plan.allowed
    assert _blocker_codes(plan) == [ResourceBlockerCode.INSUFFICIENT_CAPACITY]


def test_a_pool_not_above_the_floor_is_a_typed_wait() -> None:
    plan = plan_capacity(
        {"rank-0": _declared_demand(100)},
        [_idle_capacity(5, 5)],
        memory_floor_bytes=5,
    )
    assert not plan.allowed
    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _codes(plan)


def test_a_declared_swap_minimum_beyond_the_largest_setting_waits_for_swap() -> None:
    declared = replace(
        _declared_demand(10 * GIB),
        minimum_swap_bytes=(SWAP_POLICY.maximum_gib + 1) * GIB,
    )
    # No swap yet: a wait for swap that names the largest setting.
    plan = _plan(declared, _idle_capacity(_SPARK_POOL, _FREE))
    assert not plan.allowed
    assert _blocker_codes(plan) == [ResourceBlockerCode.INSUFFICIENT_SWAP]
    assert ResourcePlanningCode.ESTIMATE_UNCERTAIN in _codes(plan)
    (swap,) = (r for r in plan.reasons if r.swap is not None)
    assert swap.swap is not None
    assert swap.swap.needed_setting_bytes == SWAP_POLICY.maximum_gib * GIB


def _with_swap(swap: SwapCapacity):
    return memory_capacity_snapshot(
        "rank-0",
        "unified",
        host=(_SPARK_POOL, _FREE),
        accelerator=(_SPARK_POOL, _FREE),
        reservations=MemoryReservationTotals({}, {}, {}),
        memory_pool="shared",
        evidence_state="fresh",
        swap=swap,
    )


@pytest.mark.parametrize(
    "swap",
    [
        # Swap in use stays non-zero on a real Spark.
        SwapCapacity(total_bytes=SWAP_POLICY.maximum_gib * GIB, used_bytes=5 * GIB),
        # SwapTotal a page short of the setting (the kernel leaves out a header).
        SwapCapacity(total_bytes=SWAP_POLICY.maximum_gib * GIB - 4096),
    ],
    ids=["swap-in-use", "page-short"],
)
def test_a_cut_swap_rise_is_admitted_once_the_setting_is_reached(swap) -> None:
    rise = usable_swap_bytes(SWAP_POLICY.maximum_gib * GIB) + GIB
    declared = replace(_declared_demand(10 * GIB), load_overshoot_bytes=rise)
    plan = _plan(declared, _with_swap(swap))
    assert plan.allowed, [r.detail for r in plan.reasons]
    assert _blocker_codes(plan) == []


def test_a_cut_swap_rise_waits_until_the_setting_is_reached() -> None:
    rise = usable_swap_bytes(SWAP_POLICY.maximum_gib * GIB) + GIB
    declared = replace(_declared_demand(10 * GIB), load_overshoot_bytes=rise)
    plan = _plan(
        declared, _with_swap(SwapCapacity(total_bytes=64 * GIB, used_bytes=GIB))
    )
    assert _blocker_codes(plan) == [ResourceBlockerCode.INSUFFICIENT_SWAP]
