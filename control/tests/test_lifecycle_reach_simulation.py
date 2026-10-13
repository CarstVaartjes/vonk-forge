"""Seeded lifecycle simulation, reach: the parts the first one left out.

The same liveness checks as ``test_lifecycle_liveness_simulation``, plus the
reach checks (``lifecycle_simulation.reach_invariants``), over a world that
also runs the real NAS model cache with its download and removal workers, its
provider and the storage collector evicting from it
(``lifecycle_simulation.reach_world``), driven by a second action table
(``lifecycle_simulation.reach_actions``) so the first table's seeds keep their
meaning.

Replay one seed with ``pytest -k 'test_reach_seed[3]'``. A long run sets
``VONK_LIFECYCLE_REACH_SEEDS`` (how many), ``VONK_LIFECYCLE_REACH_FIRST_SEED``
and ``VONK_LIFECYCLE_REACH_STEPS``.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import select
from vonk_control.models import (
    FleetProfileApplication,
    ModelCacheOperation,
    RecipeInstallation,
)

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.reach import reach_simulation
from .lifecycle_simulation.reach_world import (
    MODEL_SELECTOR,
    ReachWorld,
    build_reach_world,
)
from .lifecycle_simulation.simulation import Simulation, SimulationFailure
from .lifecycle_simulation.world import PAIR

_COLD_NAS_LOAD = (
    "A load whose model is not on the NAS plans a model download, but the"
    " download phase asks the cache for the artifact set by digest and no set"
    " row exists yet: it fails with ModelCacheNotFoundInvalid ('cache entry was"
    " not found'), is retried five times the same way and ends failed. The"
    " model is never downloaded."
)
_REMOVAL_READS_INSTALLATIONS_AS_PLANS = (
    "Removing a model while any installation is active ends the removal failed"
    " with 'accepted Run/Switch plan is malformed; removal was deferred': the"
    " reference scan parses each installation's stored plan as a RunSwitchPlan"
    " (artifact_reference_scan.model_references), which it is not."
)
_REMOVING_A_REMOVED_MODEL = (
    "Removing a model that is no longer on the NAS is accepted, then backs off"
    " with 'model removal ownership observation unavailable' until it ends"
    " failed; it should end at once (nothing to remove)."
)
#: Known bugs by a pattern of their violation signature (see ``Violation``).
KNOWN_BUGS = {
    r"nothing fails without a fault outside the Controller: load <id> failed:"
    r" .*ModelCacheNotFoundInvalid: cache entry was not found": _COLD_NAS_LOAD,
    r"a fresh load is admitted after quiescing and serves: failed: .*"
    r"ModelCacheNotFoundInvalid: cache entry was not found": _COLD_NAS_LOAD,
    r"nothing fails without a fault outside the Controller: remove <id> failed:"
    r" accepted Run/Switch plan is malformed": _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    r"nothing fails without a fault outside the Controller: remove <id> failed:"
    r" model removal ownership observation unavailable": _REMOVING_A_REMOVED_MODEL,
}
#: Seeds of the fixed sample that find a known bug; they flip when it is fixed.
FIXED_SAMPLE_FAILURES: dict[int, str] = {
    0: _REMOVING_A_REMOVED_MODEL,
    1: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    2: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    4: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    5: _REMOVING_A_REMOVED_MODEL,
    6: _REMOVING_A_REMOVED_MODEL,
    7: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
}

_LONG_RUN = "VONK_LIFECYCLE_REACH_SEEDS" in os.environ
SEEDS = int(os.environ.get("VONK_LIFECYCLE_REACH_SEEDS", "8"))
FIRST_SEED = int(os.environ.get("VONK_LIFECYCLE_REACH_FIRST_SEED", "0"))
STEPS = int(os.environ.get("VONK_LIFECYCLE_REACH_STEPS", "25"))


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


# Each seed drives the whole worker loop and the model cache for about a
# hundred rounds over PostgreSQL.
@pytest.mark.slow(40)
@pytest.mark.parametrize("seed", _seeds())
def test_reach_seed(tmp_path, postgres_engine, seed: int) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        reach_simulation(world, seed, known=tuple(KNOWN_BUGS)).run(STEPS)
    except SimulationFailure as failure:
        known = _known(failure)
        if _LONG_RUN and known is not None:
            pytest.xfail(f"{known}\n{failure}")
        expected = None if _LONG_RUN else FIXED_SAMPLE_FAILURES.get(seed)
        if expected is not None and known != expected:
            # A recorded seed that now finds something else is a new finding.
            raise AssertionError(f"expected: {expected}\n{failure}") from failure
        raise
    finally:
        world.close()


def _known(failure: SimulationFailure) -> str | None:
    signature = failure.violation.signature
    return next(
        (bug for pattern, bug in KNOWN_BUGS.items() if re.match(pattern, signature)),
        None,
    )


# The shortest traces of the known bugs, for whoever fixes them.


def _apply(sim: Simulation, name: str, assignments: list[dict[str, object]]) -> str:
    profile_id, _number = sim.world.create_profile(name, assignments)
    application = sim.world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    )
    return application.id


def _remove(world: ReachWorld, rounds: int, sim: Simulation) -> str:
    removal = world.reach.cache.remove_model_selector(
        MODEL_SELECTOR, actor="admin", request_key=str(uuid.uuid4())
    )
    for _ in range(rounds):
        sim.round()
    return removal.id


def _request_download(world: ReachWorld, sim: Simulation) -> str:
    """A fresh operator download of the model; its last state."""

    download = world.reach.cache.download_model_selector(
        MODEL_SELECTOR, actor="admin", request_key=str(uuid.uuid4())
    )
    for _ in range(6):
        sim.round()
    return world.reach.cache.get_operation(download.id).state


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_COLD_NAS_LOAD)
def test_a_load_downloads_a_model_the_nas_lacks(tmp_path, postgres_engine) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        _apply(sim, "pair", [world.assignment(PAIR, world.nodes)])
        for _ in range(40):  # every retry the load gets, a minute apart
            sim.round(60)

        with world.sessions() as session:
            loads = dict(
                session.execute(
                    select(
                        FleetProfileApplication.state,
                        FleetProfileApplication.status_reason,
                    )
                ).all()
            )
        assert "succeeded" in loads, loads
    finally:
        world.close()


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason=_REMOVAL_READS_INSTALLATIONS_AS_PLANS
)
def test_a_removal_waits_for_the_installation_using_the_model(
    tmp_path, postgres_engine
) -> None:
    """The model stays installed after its run stops: the removal waits for it."""

    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        assert sim.settle(_apply(sim, "pair", [world.assignment(PAIR, world.nodes)]))
        assert sim.settle(_apply(sim, "idle", [])) == "succeeded"
        removal = world.reach.cache.get_operation(_remove(world, 10, sim))

        assert removal.state not in ENDED, removal
        with world.sessions() as session:
            installations = set(
                session.scalars(
                    select(RecipeInstallation.id).where(
                        RecipeInstallation.state == "installed"
                    )
                )
            )
            stored = session.get(ModelCacheOperation, removal.id)
            reason = None if stored is None else stored.last_error
        assert installations and any(item in str(reason) for item in installations)
        # A fresh load of the model is newer intent: it wins over the removal.
        again = _apply(sim, "pair again", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(again) == "succeeded"
    finally:
        world.close()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_REMOVING_A_REMOVED_MODEL)
def test_removing_a_removed_model_ends(tmp_path, postgres_engine) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        first = world.reach.cache.get_operation(_remove(world, 4, sim))
        assert first.state == "succeeded"

        again = world.reach.cache.get_operation(_remove(world, 12, sim))
        assert again.state == "succeeded", again
        assert _request_download(world, sim) == "succeeded"
    finally:
        world.close()
