"""The simulation reaches the run reaper: a Start superseded before any Spark
claims it leaves its run in ``starting``, and the recovery tick ends it.

The seeded walk (``test_lifecycle_liveness_simulation``) draws the same ending
at random; this scenario makes it certain, so a Controller whose recovery tick
does not reap fails the in-flight-owner invariant here, and one that reaps
nothing fails the assertion on the reap.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select
from vonk_control.models import RecipeRun

from .lifecycle_simulation.actions import Step, perform
from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import PAIR, SOLO, build_world

_REAPED = "recipe.run.owner-ended"


def _load(sim: Simulation, name: str, assignments: list[dict[str, object]]) -> str:
    profile_id, _number = sim.world.create_profile(name, assignments)
    return sim.world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _states(sim: Simulation) -> dict[str, str]:
    with sim.world.sessions() as session:
        return {
            run_id: state
            for run_id, state in session.execute(select(RecipeRun.id, RecipeRun.state))
        }


@pytest.mark.slow(40)
def test_a_start_superseded_by_a_removal_before_any_claim_is_reaped(
    tmp_path, postgres_engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        solo = world.assignment(SOLO, world.nodes[:1])
        # Two installations stay behind, unused, with nothing running.
        for name, assignment in (
            ("solo", solo),
            ("pair", world.assignment(PAIR, world.nodes)),
        ):
            assert sim.settle(_load(sim, name, [assignment])) == "succeeded"
            assert sim.settle(_load(sim, f"idle {name}", [])) == "succeeded"
        assert set(_states(sim).values()) == {"stopped"}

        # The Sparks report but answer no order for a while: the next load's
        # Start is queued, never claimed.
        for spark in sim.sparks.by_node.values():
            spark.late_rounds = 200
        _load(sim, "solo again", [solo])
        for _ in range(20):
            sim.round()
            if "starting" in _states(sim).values():
                break
        assert "starting" in _states(sim).values()

        # Removing the unused pair installation takes a newer intent over both
        # Sparks, which settles the unissued Start cancelled.
        perform(sim, Step("remove-unused-install", 0, 0, 0))
        for _ in range(30):
            sim.round()
        assert "starting" not in _states(sim).values()
        assert any(_REAPED in record.getMessage() for record in caplog.records)

        sim.quiesce()
        sim.fresh_load()
    finally:
        world.close()
