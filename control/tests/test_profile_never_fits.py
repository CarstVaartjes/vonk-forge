"""A load whose demand can never fit its Spark ends with that evidence.

Production (2026-10-16, Controller aed44b796, the hardware sweep):

* spark-3542 held no Vonk claim with 125.65 GB free; the aday777 recipe's
  measured 122.0 GB peak plus the 6 GB floor could not fit, and its load backed
  off 40 times on ``insufficient_capacity``, a wait nothing could clear.
* spark-2297: one load started FLUX (36 GB) and Gemma (103.4 GB) on the same
  Spark. Each fitted alone at review; after FLUX started, Gemma waited 57 times
  on the memory its own sibling held (``insufficient_reservation_budget``).

The real Controller services run over PostgreSQL on a fake clock with fake
Sparks (``lifecycle_simulation``) whose free memory is what their runs hold.
Bytes are scaled down a millionfold; the planner keeps a 50-byte floor.
"""

from __future__ import annotations

import uuid

import pytest
from vonk_agent_protocol import RunSwitchCode

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.sparks import Memory
from .lifecycle_simulation.world import SOLO, SOLO_B, World, build_world

#: spark-3542: 128 GB, 125 GB free when idle.
_SPARK = Memory(total=12_800, idle_free=12_500, held_percent=90)
_NEVER_FITS = RunSwitchCode.RESOURCE_DEMAND_EXCEEDS_CAPACITY


def _load(world: World, profile_id: str) -> str:
    return world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _ending_codes(world: World, application_id: str) -> set[str]:
    application = world.controller.profiles.application(application_id)
    return {blocker.code for blocker in application.blockers}


def _two_starts_on_one_spark(sim: Simulation, peak_bytes: int) -> str:
    world = sim.world
    world.publish_successor(SOLO, peak_bytes=peak_bytes)
    world.publish_successor(SOLO_B, peak_bytes=peak_bytes)
    sim.sparks.by_node[world.nodes[0]].memory = _SPARK
    sim.round()
    spark = world.nodes[:1]
    profile_id, _number = world.create_profile(
        "Two starts on one Spark",
        [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
    )
    return profile_id


@pytest.mark.slow(30)
def test_a_peak_no_held_memory_can_make_room_for_ends_the_load(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        # 12_500 idle free less a 12_480 peak leaves 20 of the 50-byte floor,
        # though the peak and the floor fit the 12_800 pool.
        world.publish_successor(SOLO, peak_bytes=12_480)
        spark = sim.sparks.by_node[world.nodes[0]]
        spark.memory = _SPARK
        sim.round()
        profile_id, _number = world.create_profile(
            "Too large for its Spark", [world.assignment(SOLO, world.nodes[:1])]
        )

        review = world.controller.profiles.preview(profile_id)
        assert not review.allowed
        assert _NEVER_FITS in {
            reason.code
            for item in review.assessments
            for reason in item.assessment.blockers
        }

        load = _load(world, profile_id)
        assert sim.settle(load) == "failed"
        assert _NEVER_FITS in _ending_codes(world, load)
        assert not spark.serving
    finally:
        world.close()


@pytest.mark.slow(30)
def test_starts_that_cannot_share_their_spark_are_refused_at_review(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        # Each 7_000 peak fits the idle Spark; both and the floor need 14_050.
        profile_id = _two_starts_on_one_spark(sim, 7_000)

        review = world.controller.profiles.preview(profile_id)
        assert not review.allowed
        assert [
            reason.code for reason in review.reasons if reason.severity == "error"
        ] == [_NEVER_FITS]

        load = _load(world, profile_id)
        assert sim.settle(load) == "failed"
        assert _NEVER_FITS in _ending_codes(world, load)
        assert not sim.sparks.by_node[world.nodes[0]].serving
    finally:
        world.close()


@pytest.mark.slow(30)
def test_a_load_accepted_before_the_check_ends_instead_of_waiting_on_its_sibling(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    # A load a Controller without the review check accepted (fad7d2ec): the
    # first start runs, and the second must end the load, not wait for ever.
    monkeypatch.setattr(
        "vonk_control.fleet_profiles.assessment.colocated_memory_overcommit",
        lambda *_args, **_kwargs: [],
    )
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        profile_id = _two_starts_on_one_spark(sim, 7_000)
        assert world.controller.profiles.preview(profile_id).allowed

        load = _load(world, profile_id)
        assert sim.settle(load) == "failed"
        assert _NEVER_FITS in _ending_codes(world, load)
        assert len(sim.sparks.by_node[world.nodes[0]].serving) == 1
    finally:
        world.close()
