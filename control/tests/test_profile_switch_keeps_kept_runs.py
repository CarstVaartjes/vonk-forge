"""A load that keeps a multi-Spark run while it changes one of its Sparks.

The switch step covers every Spark the plan touches, including the Sparks of a
run it keeps when that run shares a changed Spark: the kept run is part of the
picture on every Spark it spans, so the load neither loops on a cross-scope
verdict of its own making nor touches the run it keeps.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from vonk_control.fleet_profiles.control_effects import switch_step_scope
from vonk_control.models import RecipeRun, RunNode

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import PAIR, SOLO_B, World, build_world


def _settled(sim: Simulation, name: str, assignments: list[dict[str, object]]) -> str:
    profile_id, _number = sim.world.create_profile(name, assignments)
    application = sim.world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    )
    return sim.settle(application.id)


def _running(world: World) -> dict[str, frozenset[str]]:
    """Each running run's Sparks, by run id."""

    with world.sessions() as session:
        return {
            run_id: frozenset(
                session.scalars(select(RunNode.node_id).where(RunNode.run_id == run_id))
            )
            for run_id in session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
        }


def _serving(sim: Simulation) -> dict[str, set[str]]:
    return {node: set(spark.serving) for node, spark in sim.sparks.by_node.items()}


def _pair_and_solo_b(sim: Simulation) -> tuple[str, str]:
    """Load pair on both Sparks and solo-b on the first; their run ids."""

    world = sim.world
    both, first = frozenset(world.nodes), frozenset(world.nodes[:1])
    loaded = _settled(
        sim,
        "pair and solo-b",
        [
            world.assignment(PAIR, world.nodes),
            world.assignment(SOLO_B, world.nodes[:1]),
        ],
    )
    assert loaded == "succeeded"
    by_nodes = {nodes: run_id for run_id, nodes in _running(world).items()}
    return by_nodes[both], by_nodes[first]


# Three loads settle through the whole worker loop over PostgreSQL.
@pytest.mark.slow(30)
def test_dropping_a_colocated_run_keeps_the_pair(tmp_path, postgres_engine) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        pair, _solo_b = _pair_and_solo_b(sim)

        loaded = _settled(sim, "pair", [world.assignment(PAIR, world.nodes)])

        assert loaded == "succeeded"
        assert _running(world) == {pair: frozenset(world.nodes)}
        assert _serving(sim) == {node: {pair} for node in world.nodes}
        sim.fresh_load()
    finally:
        world.close()


# Three loads settle through the whole worker loop over PostgreSQL.
@pytest.mark.slow(30)
def test_dropping_the_pair_keeps_the_colocated_run(tmp_path, postgres_engine) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        _pair, solo_b = _pair_and_solo_b(sim)
        first, second = world.nodes

        loaded = _settled(sim, "solo-b", [world.assignment(SOLO_B, (first,))])

        assert loaded == "succeeded"
        assert _running(world) == {solo_b: frozenset({first})}
        assert _serving(sim) == {first: {solo_b}, second: set()}
        sim.fresh_load()
    finally:
        world.close()


def test_the_step_covers_kept_workloads_that_share_a_changed_spark() -> None:
    profile = {"a", "b", "c", "d"}
    # Two kept pairs chain from the changed Spark; a kept run elsewhere stays out.
    kept = [{"a", "b"}, {"b", "c"}, {"d"}]
    assert switch_step_scope({"a"}, kept, profile) == {"a", "b", "c"}
    # A kept run's Sparks outside the profile are never drawn in.
    assert switch_step_scope({"a"}, [{"a", "x"}], profile) == {"a"}
    # Nothing changed: no step, whatever is kept.
    assert switch_step_scope(set(), kept, profile) == set()
