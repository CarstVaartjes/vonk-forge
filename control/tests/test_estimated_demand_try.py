"""A recipe's declared peak is an estimate: it is tried, not refused.

Owner 2026-11-02: assume the creator made a working recipe, use all its
settings, measure the real usage, and do not block until it is known whether
it works. An estimate above what an idle Spark admits is therefore tried as the
only claim on an empty Spark and waits (a typed wait, never "never fits") while
anything else holds memory there. A measured demand keeps the exact rule.
"""

from __future__ import annotations

from dataclasses import replace

from vonk_agent_protocol import ResourceBlockerCode, ResourcePlanningCode
from vonk_agent_protocol.swap import GIB, SWAP_POLICY
from vonk_control.resource_planning import (
    PLATFORM_MEMORY_FLOOR_BYTES,
    PlannedStopRelease,
    plan_capacity,
)

from .test_resource_planning import (
    _SPARK_POOL,
    _blocker_codes,
    _declared_demand,
    _idle_capacity,
)

_FLOOR = PLATFORM_MEMORY_FLOOR_BYTES
_FREE = 125_650_657_280
#: Above the most an idle Spark admits, below the physical pool.
_OVER_MAX = _FREE - _FLOOR + 1_000_000_000


def _measured(total: int):
    return replace(_declared_demand(total), evidence_state="measured")


def _plan(demand, capacity, stops=()):
    return plan_capacity(
        {"rank-0": demand}, [capacity], stops, memory_floor_bytes=_FLOOR
    )


def test_a_declared_peak_is_an_estimate() -> None:
    assert not _declared_demand(_OVER_MAX).measured
    assert _measured(_OVER_MAX).measured


def test_an_estimate_above_the_idle_maximum_is_tried_on_an_empty_spark() -> None:
    plan = _plan(_declared_demand(_OVER_MAX), _idle_capacity(_SPARK_POOL, _FREE))
    assert plan.allowed
    assert _blocker_codes(plan) == []
    (node,) = plan.nodes
    # The try claims all free memory above the floor, so nothing co-locates.
    assert node.exclusive
    assert node.required_bytes == _FREE - _FLOOR
    assert node.selected_free_after_bytes == _FLOOR
    assert ResourcePlanningCode.ESTIMATE_UNCERTAIN in {r.code for r in plan.reasons}


def test_an_estimate_above_the_physical_pool_is_tried_too() -> None:
    plan = _plan(
        _declared_demand(_SPARK_POOL + 1_000_000_000),
        _idle_capacity(_SPARK_POOL, _FREE),
    )
    assert plan.allowed and plan.nodes[0].exclusive


def test_an_estimate_waits_while_the_spark_holds_anything() -> None:
    held = _plan(
        _declared_demand(_OVER_MAX),
        _idle_capacity(_SPARK_POOL, _FREE - 1_000_000_000, claimed=1_000_000_000),
    )
    assert not held.allowed
    assert _blocker_codes(held) == [ResourceBlockerCode.INSUFFICIENT_CAPACITY]
    assert not held.nodes[0].exclusive

    stopping = _plan(
        _declared_demand(_OVER_MAX),
        _idle_capacity(_SPARK_POOL, _FREE),
        [PlannedStopRelease("old", "rank-0", "unified-memory", 100_000_000, True)],
    )
    assert not stopping.allowed
    assert _blocker_codes(stopping) == [
        ResourceBlockerCode.INSUFFICIENT_CAPACITY_AFTER_STOP
    ]


def test_an_estimate_still_waits_when_the_host_leaves_nothing_free() -> None:
    # A real host fact: nothing above the floor is left to claim.
    plan = _plan(_declared_demand(_OVER_MAX), _idle_capacity(_SPARK_POOL, _FLOOR - 1))
    assert not plan.allowed
    assert _blocker_codes(plan) == [ResourceBlockerCode.INSUFFICIENT_CAPACITY]


def test_a_measured_demand_keeps_the_never_fits_rule() -> None:
    idle = _plan(_measured(_OVER_MAX), _idle_capacity(_SPARK_POOL, _FREE))
    assert not idle.allowed
    assert _blocker_codes(idle) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]
    beyond_pool = _plan(
        _measured(_SPARK_POOL), _idle_capacity(_SPARK_POOL, _FREE, claimed=GIB)
    )
    assert _blocker_codes(beyond_pool) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]


def test_a_swap_declaration_is_an_estimate_too() -> None:
    declared = replace(
        _declared_demand(10 * GIB),
        minimum_swap_bytes=(SWAP_POLICY.maximum_gib + 1) * GIB,
    )
    capacity = _idle_capacity(_SPARK_POOL, _FREE)
    tried = _plan(declared, capacity)
    assert tried.allowed
    assert ResourcePlanningCode.ESTIMATE_UNCERTAIN in {r.code for r in tried.reasons}

    measured = _plan(replace(declared, evidence_state="measured"), capacity)
    assert not measured.allowed
    assert _blocker_codes(measured) == [ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY]
