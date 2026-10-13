"""One profile load places several recipes on one Spark, and every one serves.

Production (2026-10-13, the hardware sweep's baseline beside the recipe under
test on one Spark): the load started the first recipe; the second child planned
to stop that run as if it were someone else's, was refused as "an unreviewed
workload" on every retry, and an hour later the load ended cancelled although
nobody had cancelled it.

The real Controller services run over PostgreSQL on a fake clock with fake
Sparks (``lifecycle_simulation``); the simulation's invariants are checked
after every round.
"""

from __future__ import annotations

import ast
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import vonk_control
from sqlalchemy import select
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileEffects,
    FleetProfileInstallationEffect,
    FleetProfileRunEffect,
)
from vonk_control.fleet_profiles.contracts import FleetProfileReviewStale
from vonk_control.fleet_profiles.projection_support import _validate_remaining_effects
from vonk_control.models import FleetProfileApplication

from .lifecycle_simulation import invariants
from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import PAIR, SOLO, SOLO_B, World, build_world

#: Twice the half hour after which a waiting load used to end itself.
_PAST_THE_OLD_WINDOW = timedelta(hours=1)
#: A round as long as it can be while Sparks still report within the age-out
#: of route evidence (120 s).
_WAIT_ROUND_SECONDS = 110


def _load(world: World, profile_id: str) -> str:
    return world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _loads_of(world: World, profile_id: str) -> list[FleetProfileApplicationView]:
    """Every load of a profile, oldest first: the load and the retries it handed to."""

    with world.sessions() as session:
        ids = session.scalars(
            select(FleetProfileApplication.id)
            .where(FleetProfileApplication.profile_id == profile_id)
            .order_by(FleetProfileApplication.created_at, FleetProfileApplication.id)
        ).all()
    return [world.controller.profiles.application(item) for item in ids]


def _fresh_load_serves(sim: Simulation, profile_id: str, ended: str) -> None:
    """Whatever came before, a fresh load is admitted and ends serving."""

    fresh = _load(sim.world, profile_id)
    assert fresh != ended
    assert sim.settle(fresh) == "succeeded"


# Each simulation drives the whole worker loop for some hundred rounds over
# PostgreSQL, like the production worker does.
@pytest.mark.slow(30)
def test_a_pair_on_both_sparks_and_a_recipe_beside_it_all_serve(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        first, second = world.nodes
        profile_id, _number = world.create_profile(
            "Pair with a recipe beside it",
            [world.assignment(PAIR, world.nodes), world.assignment(SOLO_B, (first,))],
        )
        load = _load(world, profile_id)

        assert sim.settle(load) == "succeeded"
        (pair_run,) = sim.sparks.by_node[second].serving
        assert sim.sparks.by_node[first].serving - {pair_run}
        _fresh_load_serves(sim, profile_id, load)
    finally:
        world.close()


@pytest.mark.slow(30)
def test_a_replacement_load_of_the_same_assignments_ends_serving(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = world.nodes[:1]
        profile_id, _number = world.create_profile(
            "Two recipes on one Spark",
            [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
        )
        first = _load(world, profile_id)
        assert sim.settle(first) == "succeeded"
        serving = set(sim.sparks.by_node[spark[0]].serving)

        replacement = _load(world, profile_id)

        assert replacement != first
        assert sim.settle(replacement) == "succeeded"
        # Both runs are its own: kept, neither stopped nor started again.
        assert sim.sparks.by_node[spark[0]].serving == serving
        assert len(serving) == 2
    finally:
        world.close()


@pytest.mark.slow(30)
def test_a_recipe_under_test_joins_a_serving_baseline_on_its_spark(
    tmp_path, postgres_engine
) -> None:
    """The hardware sweep: a baseline serves, then a profile of the baseline
    and the recipe under test on the same Spark is loaded."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = world.nodes[:1]
        baseline_profile, _number = world.create_profile(
            "Baseline", [world.assignment(SOLO, spark)]
        )
        assert sim.settle(_load(world, baseline_profile)) == "succeeded"
        (baseline_run,) = sim.sparks.by_node[spark[0]].serving
        sweep_profile, _number = world.create_profile(
            "Baseline and the recipe under test",
            [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
        )
        sweep = _load(world, sweep_profile)

        assert sim.settle(sweep) == "succeeded"
        serving = sim.sparks.by_node[spark[0]].serving
        assert len(serving) == 2 and baseline_run in serving
        _fresh_load_serves(sim, sweep_profile, sweep)
    finally:
        world.close()


@pytest.mark.slow(40)
def test_a_load_whose_second_child_waits_is_never_ended_without_a_request(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    # Waiting repeats its reason by design; only the load's end is judged here.
    monkeypatch.setattr(invariants, "REFUSAL_REPEATS", 10**6)
    monkeypatch.setattr(invariants, "STALL_WINDOW", timedelta(days=30))
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        spark = world.nodes[:1]
        profile_id, _number = world.create_profile(
            "Two recipes on one Spark",
            [world.assignment(SOLO, spark), world.assignment(SOLO_B, spark)],
        )
        _load(world, profile_id)
        silent = sim.sparks.by_node[spark[0]]
        while not silent.serving:
            sim.round()
        silent.inventory_silent = True

        # Nothing can be planned on that Spark without a current inventory; the
        # fake clock runs well past the half hour after which the load used to end.
        waited = timedelta()
        while waited < _PAST_THE_OLD_WINDOW:
            sim.round(_WAIT_ROUND_SECONDS)
            waited += timedelta(seconds=_WAIT_ROUND_SECONDS)
            for waiting in _loads_of(world, profile_id):
                assert waiting.state != "cancelled", waiting.status_reason
                assert waiting.progress.cancellation is None
        current = _loads_of(world, profile_id)[-1]
        assert current.state not in invariants.ENDED, current.status_reason

        silent.inventory_silent = False
        # Its retries carry it on: the newest load of the profile ends serving.
        for _ in range(120):
            current = _loads_of(world, profile_id)[-1]
            if current.state in invariants.ENDED:
                break
            sim.round()
        assert current.state == "succeeded", current.status_reason
        assert len(silent.serving) == 2
        _fresh_load_serves(sim, profile_id, current.id)
    finally:
        world.close()


def test_a_retry_may_replace_its_own_run_but_never_stop_another_workload() -> None:
    """Replanning recognises a run on the profile's own installation as its own."""

    node = ["spk_" + "1" * 32]
    own, foreign = str(uuid.uuid4()), str(uuid.uuid4())
    reviewed = FleetProfileEffects(runs=[], installations=[], superseded=[])

    def remaining(installation_id: str) -> FleetProfileEffects:
        return FleetProfileEffects(
            runs=[
                FleetProfileRunEffect(
                    run_id=str(uuid.uuid4()),
                    installation_id=installation_id,
                    alias="qwen",
                    node_ids=node,
                    action="stop",
                )
            ],
            installations=[
                FleetProfileInstallationEffect(
                    installation_id=own, node_ids=node, action="keep"
                )
            ],
            superseded=[],
        )

    _validate_remaining_effects(reviewed, remaining(own))
    with pytest.raises(FleetProfileReviewStale):
        _validate_remaining_effects(reviewed, remaining(foreign))


def test_only_a_cancel_request_cancels_an_issued_load() -> None:
    """Guard: no timer, retry or replan fabricates a cancel on a load's behalf.

    Covers the cancellation that stops a load's issued effects. Ending a load
    that is still waiting for admission is a separate path.
    """

    source = Path(vonk_control.__file__).parent / "fleet_profiles"
    request = source / "cancellation.py"
    fabricated = sorted(
        f"{path.relative_to(source)}:{node.lineno}"
        for path in source.rglob("*.py")
        if path != request
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
        in {"FleetProfileApplicationCancellationIntent", "request_cancel"}
    )
    assert not fabricated, f"only a cancel request cancels a load: {fabricated}"
