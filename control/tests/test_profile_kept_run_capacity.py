"""A run the plan keeps is already on its Spark: its memory is not new demand.

Production (2026-10-13, Controller 8ff89e829, the hardware sweep): a profile
review that kept the serving llama run on spark-3542 (declared 115 GB, about
103 GB held, 21.5 GiB of 128 free) refused the load on memory. The kept
assignment was fitted as if it started again (its 115 GB on top of its own
claim), and a recipe beside it was charged the kept run's whole declared peak
on top of free memory that already showed what the run holds.

The real Controller services run over PostgreSQL on a fake clock with fake
Sparks (``lifecycle_simulation``) whose free memory is what their runs hold.
Bytes are scaled down a millionfold.
"""

from __future__ import annotations

import uuid

import pytest
from vonk_agent_protocol import RunSwitchCode

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.sparks import Memory
from .lifecycle_simulation.world import SOLO, SOLO_B, World, build_world

#: spark-3542: 128 GB, about 125 GB free when idle.
_SPARK = Memory(total=12_800, idle_free=12_500, held_percent=90)
#: The baseline declares 115 GB and holds 90% of it (about 103 GB), which
#: leaves 2_150 free; the World's planner keeps a 50-byte floor.
_BASELINE_PEAK = 11_500


def _load(world: World, profile_id: str) -> str:
    return world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _baseline_serves(sim: Simulation, *, recipe_under_test_peak: int) -> str:
    """The baseline serves alone on the first Spark; its run."""

    world = sim.world
    world.publish_successor(SOLO, peak_bytes=_BASELINE_PEAK)
    world.publish_successor(SOLO_B, peak_bytes=recipe_under_test_peak)
    spark = sim.sparks.by_node[world.nodes[0]]
    spark.memory = _SPARK
    sim.round()
    baseline, _number = world.create_profile(
        "Baseline", [world.assignment(SOLO, world.nodes[:1])]
    )
    assert sim.settle(_load(world, baseline)) == "succeeded"
    (run,) = spark.serving
    return run


def _sweep_profile(world: World) -> str:
    spark = world.nodes[:1]
    profile_id, _number = world.create_profile(
        "Baseline and the recipe under test",
        [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
    )
    return profile_id


# These tests drive the whole worker loop over PostgreSQL through three profile
# loads, like the production worker does.
@pytest.mark.slow(30)
def test_a_recipe_fits_beside_the_baseline_its_plan_keeps(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        baseline_run = _baseline_serves(sim, recipe_under_test_peak=2_000)
        sweep = _sweep_profile(world)

        review = world.controller.profiles.preview(sweep)

        assert review.allowed, [item.assessment.blockers for item in review.assessments]
        assert [effect.run_id for effect in review.effects.runs] == [baseline_run]
        assert {effect.action for effect in review.effects.runs} == {"keep"}
        for item in review.assessments:
            assert item.assessment.allowed, item.assessment.blockers
            assert item.assessment.stops == []
        load = _load(world, sweep)
        assert sim.settle(load) == "succeeded"
        serving = set(sim.sparks.by_node[world.nodes[0]].serving)
        assert len(serving) == 2 and baseline_run in serving
        # A fresh load of the same profile keeps both and is admitted.
        fresh = _load(world, sweep)
        assert fresh != load
        assert sim.settle(fresh) == "succeeded"
        assert sim.sparks.by_node[world.nodes[0]].serving == serving
    finally:
        world.close()


def test_a_recipe_the_spark_has_no_room_for_is_refused_by_its_own_fit(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        # 2_150 free, less 2_150 demand, leaves nothing for the 50-byte floor.
        baseline_run = _baseline_serves(sim, recipe_under_test_peak=2_150)
        sweep = _sweep_profile(world)

        review = world.controller.profiles.preview(sweep)

        assert not review.allowed
        by_alias = {
            item.assessment.alias: item.assessment for item in review.assessments
        }
        kept, under_test = by_alias["solo-1"], by_alias["solo-b-1"]
        assert kept.allowed and kept.stops == []
        assert under_test.stops == []
        assert [(reason.code, reason.node_ids) for reason in under_test.blockers] == [
            (RunSwitchCode.RESOURCE_INSUFFICIENT_CAPACITY, [world.nodes[0]])
        ]
        assert baseline_run in sim.sparks.by_node[world.nodes[0]].serving
    finally:
        world.close()


@pytest.mark.slow(30)
def test_a_plan_that_stops_the_baseline_fits_a_recipe_as_large(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        baseline_run = _baseline_serves(sim, recipe_under_test_peak=_BASELINE_PEAK)
        replacement, _number = world.create_profile(
            "The recipe under test alone",
            [world.assignment(SOLO_B, world.nodes[:1])],
        )

        review = world.controller.profiles.preview(replacement)

        assert review.allowed, [item.assessment.blockers for item in review.assessments]
        ((_, assessment),) = (
            (item.assignment_id, item.assessment) for item in review.assessments
        )
        assert [stop.run_id for stop in assessment.stops] == [baseline_run]
        load = _load(world, replacement)
        assert sim.settle(load) == "succeeded"
        serving = set(sim.sparks.by_node[world.nodes[0]].serving)
        assert len(serving) == 1 and baseline_run not in serving
        fresh = _load(world, replacement)
        assert sim.settle(fresh) == "succeeded"
        assert sim.sparks.by_node[world.nodes[0]].serving == serving
    finally:
        world.close()
