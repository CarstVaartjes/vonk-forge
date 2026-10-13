"""Seeded lifecycle simulation: no accepted request is ever stuck.

Each seed drives the real Controller services over PostgreSQL through a random
sequence of loads, cancels, stops, restarts and Spark faults
(``lifecycle_simulation``) and checks the liveness invariants after every
round; then a fresh load of the whole fleet must reach serving. A wrong
implementation that leaves a load stuck in one phase, refuses it in a loop,
leaks a claim past its owner, raises out of a worker tick or cannot admit a
fresh load after the faults clear fails here with its seed and step trace.

Replay one seed with ``pytest -k 'test_seed[17]'``. A long run (nightly) sets
``VONK_LIFECYCLE_SIM_SEEDS`` (how many), ``VONK_LIFECYCLE_SIM_FIRST_SEED`` and
``VONK_LIFECYCLE_SIM_STEPS``; a failure matching a known bug is reported as
an expected failure there, anything else fails.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from pydantic import ValidationError

from .lifecycle_simulation.simulation import Simulation, SimulationFailure
from .lifecycle_simulation.world import PAIR, SOLO, build_world

_UNREADABLE_REVISION = (
    "A stored recipe revision the contract no longer parses reaches"
    " RecipeDefinition.model_validate as a Residue (fleet_profiles.persistence."
    "_stored_recipe): operator loads fail with an untyped ValidationError, a"
    " load that depends on it re-arms a one-second retry with no reason shown,"
    " and the Fleet profile tick raises."
)
_CROSS_GROUP_LOOP = (
    "Loading a profile that runs a single-Spark recipe on one Spark while a"
    " two-Spark run spans both is refused with run-switch.cross-group_conflict"
    " on every retry, although the profile makes the other Spark idle; the load"
    " never replaces the run and ends cancelled."
)
#: Known bugs by a pattern of their violation signature (see ``Violation``).
KNOWN_BUGS = {
    r"no worker tick raises: FleetProfileService\.tick: ValidationError": (
        _UNREADABLE_REVISION
    ),
    r"operator requests are answered, not crashed: ValidationError": (
        _UNREADABLE_REVISION
    ),
    r"no refusal repeats without a change elsewhere: load <id> running # times: $": (
        _UNREADABLE_REVISION
    ),
    r"no refusal repeats without a change elsewhere: load <id> queued # times:"
    r" Waiting for current Fleet conditions: run-switch\.cross-group_conflict": (
        _CROSS_GROUP_LOOP
    ),
}
#: Seeds of the fixed sample that find a known bug; they flip when it is fixed.
FIXED_SAMPLE_FAILURES: dict[int, str] = {
    2: _UNREADABLE_REVISION,
    3: _CROSS_GROUP_LOOP,
    8: _UNREADABLE_REVISION,
    9: _UNREADABLE_REVISION,
    11: _UNREADABLE_REVISION,
}

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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_CROSS_GROUP_LOOP)
def test_a_single_spark_load_replaces_a_run_across_both_sparks(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        pair = _loaded(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(pair) == "succeeded"
        solo = _loaded(sim, "solo", [world.assignment(SOLO, world.nodes[:1])])
        intended = world.controller.profiles.application(solo).progress
        assert intended.intended_profile is not None
        # The accepted profile covers both Sparks: the second one is idle.
        assert set(intended.intended_profile.scope.node_ids) == set(world.nodes)

        assert sim.settle(solo) == "succeeded"
    finally:
        world.close()


@pytest.mark.xfail(strict=True, raises=ValidationError, reason=_UNREADABLE_REVISION)
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
