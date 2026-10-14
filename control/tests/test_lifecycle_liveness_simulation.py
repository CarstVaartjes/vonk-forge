"""Seeded lifecycle simulation: no accepted request is ever stuck.

Each seed drives the real Controller services over PostgreSQL through a random
sequence of loads, cancels, stops, restarts and Spark faults
(``lifecycle_simulation``) and checks the liveness invariants after every
round; then a fresh load of the whole fleet must reach serving. A wrong
implementation that leaves a load stuck in one phase, refuses it in a loop,
ends it cancelled without a request, leaks a claim past its owner, raises out
of a worker tick or cannot admit a fresh load after the faults clear fails here
with its seed and step trace.

Replay one seed with ``pytest -k 'test_seed[17]'``. A long run (nightly) sets
``VONK_LIFECYCLE_SIM_SEEDS`` (how many), ``VONK_LIFECYCLE_SIM_FIRST_SEED`` and
``VONK_LIFECYCLE_SIM_STEPS``; a failure matching a known bug is reported as
an expected failure there, anything else fails.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import RunState
from vonk_control.models import ACTIVE_RUN_STATES, RecipeRun

from .lifecycle_simulation import invariants
from .lifecycle_simulation.simulation import Simulation, SimulationFailure
from .lifecycle_simulation.world import PAIR, SOLO, SOLO_B, World, build_world

_WAITS_BEHIND_CONTINUING = (
    "A newer load waits on every retry for 'exact continuing assignments to"
    " finish under the selected profile', queued (seed 19 of the long run, not"
    " reproducible run to run) or running (seed 53) after a co-located load"
    " and an unreadable revision; the root cause is not isolated."
)
_SILENT_REPLAN = (
    "A load whose run-switch child must re-plan (its Spark was short of disk)"
    " after its recipe revision became unreadable never finishes: the child"
    " retries run-switch.plan-refresh-unavailable on the old revision instead of"
    " following the successor, and the load waits with an empty status reason"
    " (seed 10; test_a_load_whose_revision_turns_unreadable_while_it_waits_for_disk_names_it)."
)
#: Known bugs by a pattern of their violation signature (see ``Violation``).
KNOWN_BUGS = {
    r"no refusal repeats without a change elsewhere: load <id> (queued|running)"
    r" # times: Waiting for exact continuing assignments to finish under the selected"
    r" profile": _WAITS_BEHIND_CONTINUING,
    r"no refusal repeats without a change elsewhere: load <id> running # times: $": (
        _SILENT_REPLAN
    ),
}
#: Seeds of the fixed sample that find a known bug; they flip when it is fixed.
FIXED_SAMPLE_FAILURES: dict[int, str] = {10: _SILENT_REPLAN}

_LONG_RUN = "VONK_LIFECYCLE_SIM_SEEDS" in os.environ
SEEDS = int(os.environ.get("VONK_LIFECYCLE_SIM_SEEDS", "12"))
FIRST_SEED = int(os.environ.get("VONK_LIFECYCLE_SIM_FIRST_SEED", "0"))
STEPS = int(os.environ.get("VONK_LIFECYCLE_SIM_STEPS", "30"))


def _seeds() -> list[object]:
    seeds = range(FIRST_SEED, FIRST_SEED + SEEDS)
    if _LONG_RUN:
        return list(seeds)
    return [
        pytest.param(
            seed,
            marks=pytest.mark.xfail(
                strict=True,
                raises=SimulationFailure,
                reason=FIXED_SAMPLE_FAILURES[seed],
            ),
        )
        if seed in FIXED_SAMPLE_FAILURES
        else seed
        for seed in seeds
    ]


# Each seed drives the whole worker loop for about a hundred rounds over
# PostgreSQL; the sample is sized so the file stays well under a minute.
@pytest.mark.slow(40)
@pytest.mark.parametrize("seed", _seeds())
def test_seed(tmp_path, postgres_engine, seed: int) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        Simulation(world, seed).run(STEPS)
    except SimulationFailure as failure:
        signature = failure.violation.signature
        known = next(
            (
                bug
                for pattern, bug in KNOWN_BUGS.items()
                if re.match(pattern, signature)
            ),
            None,
        )
        if _LONG_RUN and known is not None:
            pytest.xfail(f"{known}\n{failure}")
        expected = None if _LONG_RUN else FIXED_SAMPLE_FAILURES.get(seed)
        if expected is not None and known != expected:
            # A recorded seed that now finds something else is a new finding.
            raise AssertionError(f"expected: {expected}\n{failure}") from failure
        raise
    finally:
        world.close()


# The shortest traces of the known bugs, for whoever fixes them.


def _loaded(sim: Simulation, name: str, assignments: list[dict[str, object]]) -> str:
    profile_id, _number = sim.world.create_profile(name, assignments)
    application = sim.world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    )
    return application.id


def _serving(sim: Simulation) -> list[set[str]]:
    return [set(sim.sparks.by_node[node].serving) for node in sim.world.nodes]


# Three loads settle in turn through the worker loop over PostgreSQL.
@pytest.mark.slow(20)
def test_a_single_spark_load_replaces_a_run_across_both_sparks(
    tmp_path, postgres_engine
) -> None:
    """The load is the authority for both Sparks: the pair run is stopped whole."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        pair = _loaded(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(pair) == "succeeded"
        [pair_runs, also_pair] = _serving(sim)
        assert len(pair_runs) == 1 and also_pair == pair_runs
        solo = _loaded(sim, "solo", [world.assignment(SOLO, world.nodes[:1])])
        intended = world.controller.profiles.application(solo).progress
        assert intended.intended_profile is not None
        # The accepted profile covers both Sparks: the second one is idle.
        assert set(intended.intended_profile.scope.node_ids) == set(world.nodes)

        assert sim.settle(solo) == "succeeded"
        [solo_runs, idle] = _serving(sim)
        assert len(solo_runs) == 1 and solo_runs != pair_runs
        assert idle == set()
        with world.sessions() as session:
            [pair_run] = pair_runs
            stopped = session.get(RecipeRun, pair_run)
            assert stopped is not None and stopped.state == RunState.STOPPED
        again = _loaded(sim, "pair again", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(again) == "succeeded"
    finally:
        world.close()


# Two loads settle in turn through the worker loop over PostgreSQL.
@pytest.mark.slow(20)
def test_a_load_across_both_sparks_replaces_a_single_spark_run(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        solo = _loaded(sim, "solo", [world.assignment(SOLO, world.nodes[:1])])
        assert sim.settle(solo) == "succeeded"
        [solo_runs, idle] = _serving(sim)
        assert len(solo_runs) == 1 and idle == set()

        pair = _loaded(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(pair) == "succeeded"
        [pair_runs, also_pair] = _serving(sim)
        assert len(pair_runs) == 1 and also_pair == pair_runs != solo_runs
    finally:
        world.close()


def test_a_saved_profile_reloads_after_its_revision_became_unreadable(
    tmp_path, postgres_engine
) -> None:
    """The catalog publishes a successor; loading the saved profile again uses it."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        profile_id, _number = world.create_profile(
            "pair", [world.assignment(PAIR, world.nodes)]
        )
        profiles = world.controller.profiles
        first = profiles.apply(profile_id, request_key=str(uuid.uuid4()), actor="admin")
        assert sim.settle(first.id) == "succeeded"
        world.make_unreadable(PAIR)

        again = profiles.apply(profile_id, request_key=str(uuid.uuid4()), actor="admin")
        assert sim.settle(again.id) == "succeeded"
    finally:
        world.close()


# Four loads through the whole worker loop over PostgreSQL, one tick a round.
@pytest.mark.slow(40)
def test_an_unreadable_revision_holds_back_only_loads_of_its_own_recipe(
    tmp_path, postgres_engine
) -> None:
    """No readable revision of a serving recipe: every tick still runs (the
    monitor fails a raising one), its run keeps serving, another profile loads,
    a load of it waits naming the revision and cause, and it loads once a
    readable revision is published."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        profiles = world.controller.profiles
        first, second = world.nodes
        both = _loaded(
            sim,
            "both",
            [
                world.assignment(SOLO, (first,)),
                world.assignment(SOLO_B, (second,)),
            ],
        )
        assert sim.settle(both) == "succeeded"
        serving = {node: set(sim.sparks.by_node[node].serving) for node in world.nodes}
        profile_id = profiles.application(both).profile_id

        world.make_unreadable(SOLO, successor=False)
        for _ in range(5):
            sim.round()
        assert {
            node: sim.sparks.by_node[node].serving for node in world.nodes
        } == serving

        other = _loaded(sim, "second only", [world.assignment(SOLO_B, (second,))])
        assert sim.settle(other) == "succeeded"

        waiting = profiles.apply(
            profile_id, request_key=str(uuid.uuid4()), actor="admin"
        )
        assert waiting.state == "queued"
        assert any(
            blocker.code == "profile.recipe_unavailable"
            and re.search(r"revision \S+ is unreadable", blocker.detail)
            for blocker in waiting.progress.blockers
        )

        world.publish_successor(SOLO)
        assert sim.settle(waiting.id) == "succeeded"
        assert sim.sparks.by_node[first].serving
        sim.fresh_load()
    finally:
        world.close()


# Settles a waiting two-recipe load, a catalog republish and the resumed load
# through the worker loop over PostgreSQL.
@pytest.mark.slow(60)
def _waiting_for_solo(sim: Simulation) -> tuple[str, set[str]]:
    """SOLO_B serves on the second Spark and SOLO has no readable revision; a
    load of both is accepted and waits. Its id, and the runs on that Spark."""

    world = sim.world
    first, second = world.nodes
    serving = _loaded(sim, "second", [world.assignment(SOLO_B, (second,))])
    assert sim.settle(serving) == "succeeded"
    world.make_unreadable(SOLO, successor=False)
    waiting = _loaded(
        sim,
        "both",
        [world.assignment(SOLO, (first,)), world.assignment(SOLO_B, (second,))],
    )
    for _ in range(3):
        sim.round()
    application = world.controller.profiles.application(waiting)
    assert application.state == "queued"
    assert any(
        blocker.code == "profile.recipe_unavailable"
        for blocker in application.progress.blockers
    )
    return waiting, set(sim.sparks.by_node[second].serving)


# Three loads and a fresh one through the whole worker loop over PostgreSQL.
@pytest.mark.slow(25)
def test_a_load_waiting_for_a_readable_revision_loads_once_one_is_published(
    tmp_path, postgres_engine
) -> None:
    """The same accepted load resumes on every Spark of its profile: the
    second Spark, untouched by its plan, keeps its run and is not mistaken
    for a newer intent's."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        first, second = world.nodes
        waiting, kept = _waiting_for_solo(sim)
        world.publish_successor(SOLO)
        assert sim.settle(waiting) == "succeeded"
        assert sim.sparks.by_node[first].serving
        assert sim.sparks.by_node[second].serving == kept
        sim.fresh_load()
    finally:
        world.close()


# Three loads and a fresh one through the whole worker loop over PostgreSQL.
@pytest.mark.slow(25)
def test_a_newer_load_supersedes_a_load_waiting_for_a_readable_revision(
    tmp_path, postgres_engine
) -> None:
    """A newer load supersedes the waiting one when it is admitted, although
    the waiting plan has no step on a Spark yet: it never fences one later."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        _first, second = world.nodes
        waiting, _kept = _waiting_for_solo(sim)
        newer = _loaded(sim, "second again", [world.assignment(SOLO_B, (second,))])
        assert sim.settle(newer) == "succeeded"
        replaced = world.controller.profiles.application(waiting)
        assert replaced.state == "superseded"
        assert replaced.progress.superseded_by == newer
        assert replaced.progress.workload_intent_ordinal is None
        world.publish_successor(SOLO)
        sim.fresh_load()
    finally:
        world.close()


def test_one_load_places_two_recipes_on_one_spark(tmp_path, postgres_engine) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = world.nodes[:1]
        stacked = _loaded(
            sim,
            "stacked",
            [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
        )
        assert sim.settle(stacked) == "succeeded"
        assert len(sim.sparks.by_node[spark[0]].serving) == 2
    finally:
        world.close()


def _stacked(world: World) -> str:
    """A saved profile placing two different recipes on the first Spark."""

    spark = world.nodes[:1]
    profile_id, _number = world.create_profile(
        "stacked", [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)]
    )
    return profile_id


def _apply(world: World, profile_id: str) -> str:
    application = world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    )
    return application.id


# Two loads through the whole worker loop over PostgreSQL, a few dozen rounds.
@pytest.mark.slow(30)
def test_a_colocated_load_loaded_again_while_starting_ends_serving(
    tmp_path, postgres_engine
) -> None:
    """The newer load replaces the older one's intent and carries on: it keeps
    the serving run and starts the child the older load had not started."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = sim.sparks.by_node[world.nodes[0]]
        profile_id = _stacked(world)
        older = _apply(world, profile_id)
        while not spark.serving:
            sim.round()
        (serving_run,) = spark.serving

        again = _apply(world, profile_id)

        assert sim.settle(again) == "succeeded"
        assert world.controller.profiles.application(older).state == "superseded"
        assert len(spark.serving) == 2
        assert serving_run in spark.serving
    finally:
        world.close()


# Two loads through the whole worker loop over PostgreSQL, a few dozen rounds.
@pytest.mark.slow(30)
def test_a_colocated_load_loaded_again_before_any_child_serves_ends_serving(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = sim.sparks.by_node[world.nodes[0]]
        profile_id = _stacked(world)
        older = _apply(world, profile_id)
        profiles = world.controller.profiles
        while profiles.application(older).state != "running":
            sim.round()
        assert not spark.serving

        again = _apply(world, profile_id)

        assert sim.settle(again) == "succeeded"
        assert profiles.application(older).state == "superseded"
        assert len(spark.serving) == 2
    finally:
        world.close()


# Two loads through the whole worker loop over PostgreSQL, a few dozen rounds.
@pytest.mark.slow(30)
def test_a_different_load_replaces_a_colocated_load_while_starting(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        first, second = (sim.sparks.by_node[node] for node in world.nodes)
        older = _apply(world, _stacked(world))
        while not first.serving:
            sim.round()

        elsewhere = _loaded(sim, "solo", [world.assignment(SOLO, world.nodes[1:])])

        assert sim.settle(elsewhere) == "succeeded"
        profiles = world.controller.profiles
        assert profiles.application(older).state == "superseded"
        assert not first.serving
        assert len(second.serving) == 1
        sim.fresh_load()
    finally:
        world.close()


#: Rounds as long as they can be while Sparks still report within the age-out
#: of route evidence (120 s): a waiting load needs no finer clock.
_WAIT_ROUND_SECONDS = 110


# Two hours of the worker loop over PostgreSQL.
@pytest.mark.slow(30)
def test_a_load_nobody_cancelled_does_not_end_cancelled(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    """The co-located load again, with the refusal and stall checks relaxed so
    only its end is judged: nobody cancels it and no newer load replaces it."""

    monkeypatch.setattr(invariants, "REFUSAL_REPEATS", 10**6)
    monkeypatch.setattr(invariants, "STALL_WINDOW", timedelta(days=30))
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = world.nodes[:1]
        stacked = _loaded(
            sim,
            "stacked",
            [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
        )
        waited = timedelta()
        while waited < timedelta(hours=2):
            sim.round(_WAIT_ROUND_SECONDS)
            waited += timedelta(seconds=_WAIT_ROUND_SECONDS)
        assert world.controller.profiles.application(stacked).state != "cancelled"
    finally:
        world.close()


# Three loads through the whole worker loop over PostgreSQL, a few dozen rounds.
@pytest.mark.slow(30)
@pytest.mark.parametrize("stop_in_flight", [False, True])
def test_an_idle_load_stops_what_an_older_load_still_places(
    tmp_path, postgres_engine, stop_in_flight: bool
) -> None:
    """Reach seed 10: a load of solo on each Spark is replacing the pair run
    (stopped, or still stopping when Spark 1 answers late) and has placed no
    solo run yet when the idle profile is loaded. Newer intent wins: the older
    load is superseded, and the idle load succeeds with nothing running (the
    monitor also checks every succeeded load against the fleet)."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        pair = _loaded(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(pair) == "succeeded"
        if stop_in_flight:
            sim.sparks.spark(0).late_rounds = 4
        older = _loaded(
            sim,
            "solo-both",
            [
                {**world.assignment(SOLO, (node,)), "assignment_name": f"solo-{index}"}
                for index, node in enumerate(world.nodes, start=1)
            ],
        )
        sim.round()
        stopping = RunState.STOPPING if stop_in_flight else RunState.STOPPED
        assert _runs_in(world, stopping)
        assert _runs_in(world, *ACTIVE_RUN_STATES) == _runs_in(world, RunState.STOPPING)

        idle = _loaded(sim, "idle", [])

        assert sim.settle(idle) == "succeeded"
        assert world.controller.profiles.application(older).state == "superseded"
        assert _serving(sim) == [set(), set()]
        assert not _runs_in(world, *ACTIVE_RUN_STATES)
    finally:
        world.close()


# Two loads through the whole worker loop over PostgreSQL, a few dozen rounds.
@pytest.mark.slow(30)
def test_a_load_of_one_of_two_stacked_recipes_stops_the_other(
    tmp_path, postgres_engine
) -> None:
    """The older load runs pair on both Sparks and solo-b on Spark 1, both
    running, when the newer one, pair alone, is loaded. The newer load does not
    continue the older one (which would carry on with solo-b under it): it ends
    with one pair run serving on both Sparks and solo-b stopped."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        older = _loaded(
            sim,
            "pair + solo-b",
            [
                world.assignment(PAIR, world.nodes),
                world.assignment(SOLO_B, world.nodes[:1]),
            ],
        )
        for _ in range(30):
            sim.round()
            if len(_runs_in(world, RunState.RUNNING)) == 2:
                break
        assert len(_runs_in(world, RunState.RUNNING)) == 2

        newer = _loaded(sim, "pair", [world.assignment(PAIR, world.nodes)])

        assert sim.settle(newer) == "succeeded"
        assert world.controller.profiles.application(older).state == "superseded"
        first, second = _serving(sim)
        assert first == second and len(first) == 1
    finally:
        world.close()


# One load through the whole worker loop over PostgreSQL, a few dozen rounds.
@pytest.mark.slow(20)
@pytest.mark.xfail(strict=True, raises=SimulationFailure, reason=_SILENT_REPLAN)
def test_a_load_whose_revision_turns_unreadable_while_it_waits_for_disk_names_it(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        sim.sparks.spark(1).pressure_steps = 3
        load = _loaded(sim, "solo-2", [world.assignment(SOLO, world.nodes[1:])])
        for _ in range(3):
            sim.round()
            sim.sparks.end_step()
        world.make_unreadable(SOLO)
        sim.sparks.clear_faults()

        assert sim.settle(load) == "succeeded"
    finally:
        world.close()


def _runs_in(world: World, *states: str) -> list[str]:
    with world.sessions() as session:
        return sorted(
            session.scalars(select(RecipeRun.id).where(RecipeRun.state.in_(states)))
        )
