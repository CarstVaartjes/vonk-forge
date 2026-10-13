"""Seeded lifecycle simulation, reach: the parts the first one left out.

The same liveness checks as ``test_lifecycle_liveness_simulation``, plus the
reach checks (``lifecycle_simulation.reach_invariants``), over a world that
also runs the real NAS model cache with its download and removal workers, its
provider and the storage collector evicting from it, fake Sparks on the agent
order queue's claim and lease path, and the distributed-recovery coordinator
(``lifecycle_simulation.reach_world``, ``reach_sparks``), driven by a second
action table (``lifecycle_simulation.reach_actions``) so the first table's
seeds keep their meaning. Known bugs do not end a seed early; it fails at the
end with the first one it met.

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
    AgentOperation,
    FleetProfileApplication,
    ModelCacheOperation,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.reach import reach_simulation
from .lifecycle_simulation.reach_sparks import ReachSparks
from .lifecycle_simulation.reach_world import (
    MODEL_SELECTOR,
    ReachWorld,
    build_reach_world,
)
from .lifecycle_simulation.simulation import Simulation, SimulationFailure
from .lifecycle_simulation.world import PAIR, SOLO
from .test_lifecycle_liveness_simulation import _UNREADABLE_REVISION
from .test_lifecycle_liveness_simulation import KNOWN_BUGS as FIRST_KNOWN_BUGS

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
_RACED_MODEL_DOWNLOAD = (
    "A load planned while its model was missing from the NAS, whose model"
    " another download (an operator's) then completes, never finishes: each"
    " retry of its model-download phase downloads 0 bytes, the receipt check"
    " wants exactly the planned missing bytes, and it retries for ever with"
    " run-switch.model-download-byte-evidence-mismatch on the same stale plan."
)
_SUPERSEDED_ORDER_NEVER_ENDS = (
    "An order whose Spark went silent after claiming it (its report lost) and"
    " that newer intent then superseded stays running for ever: the"
    " lapsed-lease sweep skips orders of an older workload intent"
    " (agent_jobs/reconcile.py) and only that order's own heartbeat ends it."
    " A superseded Stop stuck this way keeps its run stopping, and a fresh"
    " load is refused in a loop (run-switch.stop-plan-unavailable)."
)
_ENDED_RUN_KEEPS_A_RANK = (
    "When one Spark's processes of a two-Spark run die, the run is settled"
    " failed (newer workload intent took its Sparks) but its surviving rank is"
    " never stopped: the Controller's own records keep it running on the other"
    " Spark, beside the run that replaced it."
)
_CRASHED_RUN_BLOCKS_THE_NEXT_LOAD = (
    "After a Spark's serving processes die (it reports it serves nothing), the"
    " next load that has to stop the crashed run ends failed with"
    " 'RecipeRequestInvalid: stop plan is stale or blocked' instead of stopping"
    " what is left of it (or skipping a run with nothing left) and starting."
)
_SILENT_RETRY_LOOP = (
    "A running load re-arms its retry on every tick with an empty status"
    " reason, so the wait names nothing. Seen with the raced model download"
    " (whose reason appears only later as"
    " run-switch.model-download-byte-evidence-mismatch); other causes are not"
    " ruled out."
)
_UNCONFIRMED_TICK_RAISE = (
    "Seen once (seed 245 of a long run, not reproducible run to run, after"
    " lost and duplicate reports and a runtime crash): FleetProfileService.tick"
    " raises FleetProfileInvalid 'Fleet profile request key was reused for"
    " another plan'. The root cause is not isolated."
)
_UNCONFIRMED_SILENT_STALL = (
    "Seen once (seed 257 of a long run, not reproducible run to run, after a"
    " runtime crash and stalled work): a load stays running with no reason and"
    " nothing changing for two hours. The root cause is not isolated."
)
#: Known bugs by a pattern of their violation signature (see ``Violation``),
#: the first simulation's among them (its unreadable revisions do not occur
#: here): an old bug met here is not a new finding.
KNOWN_BUGS = {
    **{
        pattern: bug
        for pattern, bug in FIRST_KNOWN_BUGS.items()
        if bug != _UNREADABLE_REVISION
    },
    r"nothing fails without a fault outside the Controller: load <id> failed:"
    r" .*ModelCacheNotFoundInvalid: cache entry was not found": _COLD_NAS_LOAD,
    r"a fresh load is admitted after quiescing and serves: \w+: .*"
    r"ModelCacheNotFoundInvalid: cache entry was not found": _COLD_NAS_LOAD,
    r"every request ends or progresses: unfinished after quiescing: .*"
    r"run-switch\.model-download-byte-evidence-mismatch": _RACED_MODEL_DOWNLOAD,
    r"no refusal repeats without a change elsewhere: load <id> running # times: $": (
        _SILENT_RETRY_LOOP
    ),
    r"nothing fails without a fault outside the Controller: load <id> failed:"
    r" .*RecipeRequestInvalid: stop plan is stale or blocked": (
        _CRASHED_RUN_BLOCKS_THE_NEXT_LOAD
    ),
    r"no worker tick raises: FleetProfileService\.tick: FleetProfileInvalid: Fleet"
    r" profile request key was reused for another plan": _UNCONFIRMED_TICK_RAISE,
    r"every request ends or progresses: load <id> stuck running since .*: $": (
        _UNCONFIRMED_SILENT_STALL
    ),
    r"an order whose lease lapsed is reclaimed: ": _SUPERSEDED_ORDER_NEVER_ENDS,
    r"no refusal repeats without a change elsewhere: load <id> queued # times:"
    r" Waiting for current Fleet conditions: run-switch\.stop-plan-unavailable": (
        _SUPERSEDED_ORDER_NEVER_ENDS
    ),
    r"a run that ended leaves no rank running: ": _ENDED_RUN_KEEPS_A_RANK,
    r"nothing fails without a fault outside the Controller: remove <id> failed:"
    r" accepted Run/Switch plan is malformed": _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    r"nothing fails without a fault outside the Controller: remove <id> failed:"
    r" model removal ownership observation unavailable": _REMOVING_A_REMOVED_MODEL,
}
#: Seeds of the fixed sample that find a known bug; they flip when it is fixed.
FIXED_SAMPLE_FAILURES: dict[int, str] = {
    0: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    1: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
    3: _REMOVAL_READS_INSTALLATIONS_AS_PLANS,
}

_LONG_RUN = "VONK_LIFECYCLE_REACH_SEEDS" in os.environ
SEEDS = int(os.environ.get("VONK_LIFECYCLE_REACH_SEEDS", "6"))
FIRST_SEED = int(os.environ.get("VONK_LIFECYCLE_REACH_FIRST_SEED", "0"))
STEPS = int(os.environ.get("VONK_LIFECYCLE_REACH_STEPS", "20"))


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


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
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


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
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


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
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


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_RACED_MODEL_DOWNLOAD)
def test_a_load_finishes_when_another_download_brings_its_model(
    tmp_path, postgres_engine
) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        assert world.reach.cache.get_operation(_remove(world, 4, sim)).state == (
            "succeeded"
        )
        # The operator's download and the load arrive together: the load is
        # planned while the model is still missing.
        world.reach.cache.download_model_selector(
            MODEL_SELECTOR, actor="admin", request_key=str(uuid.uuid4())
        )
        load = _apply(sim, "pair", [world.assignment(PAIR, world.nodes)])

        assert sim.settle(load) == "succeeded"
    finally:
        world.close()


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason=_SUPERSEDED_ORDER_NEVER_ENDS
)
def test_a_superseded_order_of_a_silent_spark_ends(tmp_path, postgres_engine) -> None:
    """Spark 2 claims its install and its report is lost; a newer load leaves
    Spark 2 idle. The orphaned order ends; it does not stay running."""

    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        assert isinstance(sim.sparks, ReachSparks)
        silent = sim.sparks.leases[world.nodes[1]]
        silent.lose_steps = 1
        _apply(sim, "pair", [world.assignment(PAIR, world.nodes)])
        for _ in range(12):
            sim.round()
            if _running_orders(world, world.nodes[1]):
                break
        assert _running_orders(world, world.nodes[1])
        silent.lose_steps = 0
        _apply(sim, "solo", [world.assignment(SOLO, world.nodes[:1])])
        for _ in range(140):  # past the lease, the recovery budget and a margin
            sim.round()

        assert not _running_orders(world, world.nodes[1])
    finally:
        world.close()


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_ENDED_RUN_KEEPS_A_RANK)
def test_a_run_that_lost_a_rank_leaves_nothing_running(
    tmp_path, postgres_engine
) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        assert isinstance(sim.sparks, ReachSparks)
        load = _apply(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(load) == "succeeded"
        sim.sparks.crash(1)
        for _ in range(140):
            sim.round()

        with world.sessions() as session:
            orphans = session.execute(
                select(RunNode.run_id, RunNode.node_id)
                .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                .where(
                    RecipeRun.state.in_(("failed", "stopped")),
                    RunNode.state.in_(("running", "starting")),
                )
            ).all()
        assert not orphans
        assert sim.serving_everywhere()
    finally:
        world.close()


def _running_orders(world: ReachWorld, node_id: str) -> list[str]:
    with world.sessions() as session:
        return list(
            session.scalars(
                select(AgentOperation.kind).where(
                    AgentOperation.node_id == node_id,
                    AgentOperation.state == "running",
                )
            )
        )


# A repro drives the whole worker loop, the cache and the queue for up to
# about seventy minutes of fake time over PostgreSQL.
@pytest.mark.slow(30)
@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason=_CRASHED_RUN_BLOCKS_THE_NEXT_LOAD
)
def test_a_load_after_a_runtime_crash_starts(tmp_path, postgres_engine) -> None:
    world = build_reach_world(tmp_path, postgres_engine)
    try:
        sim = reach_simulation(world, seed=0)
        assert isinstance(sim.sparks, ReachSparks)
        first = _apply(sim, "pair", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(first) == "succeeded"
        sim.sparks.crash(1)
        sim.round()

        again = _apply(sim, "pair again", [world.assignment(PAIR, world.nodes)])
        assert sim.settle(again, rounds=150) == "succeeded"
        assert sim.serving_everywhere()
    finally:
        world.close()
