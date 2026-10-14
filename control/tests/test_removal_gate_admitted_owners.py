"""A model removal never blocks the work it waits for.

A removal reserves the model's gate and then waits for every owner already
using the model (a run, its installation, an admitted load). Those owners must
still be able to act at the gate, or each waits on the other for ever: here a
Stop of the run the removal waits for (reach simulation seed 10 of a 24-seed
run, where the idle profile's Stop repeated "a model artifact set is reserved
for removal").
"""

from __future__ import annotations

import uuid

import pytest

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.reach import reach_simulation
from .lifecycle_simulation.reach_world import (
    MODEL_SELECTOR,
    ReachWorld,
    build_reach_world,
)
from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import PAIR


def _apply(sim: Simulation, name: str, assignments: list[dict[str, object]]) -> str:
    profile_id, _number = sim.world.create_profile(name, assignments)
    return sim.world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _remove(world: ReachWorld) -> str:
    return world.reach.cache.remove_model_selector(
        MODEL_SELECTOR, actor="admin", request_key=str(uuid.uuid4())
    ).id


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
def test_a_stop_ends_while_a_removal_waits_for_its_run(
    tmp_path, postgres_engine
) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        assert sim.settle(_apply(sim, "pair", [world.assignment(PAIR, world.nodes)]))
        # The removal reserves the model's gate and waits for the run.
        removal = _remove(world)
        sim.round()
        assert world.reach.cache.get_operation(removal).state not in ENDED

        assert sim.settle(_apply(sim, "idle", [])) == "succeeded"
    finally:
        world.close()
