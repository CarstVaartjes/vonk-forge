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
from .lifecycle_simulation.world import PAIR, SOLO, SOLO_B, build_world

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
    tmp_path,
    postgres_engine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO)
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        solo = world.assignment(SOLO, world.nodes[:1])
        beside = world.assignment(SOLO_B, world.nodes[:1])
        # An installation stays behind, unused, with nothing running.
        pair = _load(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(pair) == "succeeded"
        assert sim.settle(_load(sim, "idle", [])) == "succeeded"
        # Another run keeps serving on the Spark: its report is never empty, so
        # no absence of the new run can be inferred from it.
        assert sim.settle(_load(sim, "beside", [beside])) == "succeeded"

        # The Sparks answer everything but Starts for now: the next load's Start
        # is queued and never claimed.
        orders = sim.sparks._orders

        def without_starts(node_id: str):
            return tuple(
                order for order in orders(node_id) if order.kind != "recipe.start"
            )

        monkeypatch.setattr(sim.sparks, "_orders", without_starts)
        _load(sim, "solo beside", [beside, solo])
        for _ in range(60):
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
        assert "running" in _states(sim).values()  # the run beside it is untouched
        assert any(_REAPED in record.getMessage() for record in caplog.records)

        monkeypatch.undo()  # the Sparks claim Starts again
        sim.quiesce()
        sim.fresh_load()
    finally:
        world.close()
