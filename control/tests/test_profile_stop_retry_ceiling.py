"""A profile Stop that is refused the same way every time waits visibly, then ends.

A load that stops a run re-plans its Stop when the Stop's acceptance finds the
reviewed plan stale. If every fresh plan is refused the same way, the wait is
never forever and never silent: every load in the lineage shows the refusal as
a named retry blocker, each Run/Switch child ends once its checkpoint window
closes, and the
profile's retry lineage ends ``failed`` naming the refusal once it failed the
same way as often as recovery allows. Nothing is stopped on a guess.

Wrong implementation: a profile Stop child retried under the profile's backoff
without a window, so a Stop that can never be accepted held the load forever.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.fleet_profiles import PROFILE_REPEATED_FAILURE_CODE
from vonk_control.models import FleetProfileApplication, RecipeRun
from vonk_control.operation_blockers import PHASE_RETRY_CODE
from vonk_control.recovery_policy import RECOVERY

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, build_world

ROUND_SECONDS = 120


@pytest.mark.slow(30)  # a lineage of Stop children, each held for its whole window
def test_a_profile_stop_refused_every_time_waits_visibly_and_then_ends(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        served, _number = world.create_profile(
            "Served", [world.assignment(SOLO, world.nodes[:1])]
        )
        load = world.controller.profiles.apply(
            served, request_key=str(uuid.uuid4()), actor="admin"
        ).id
        assert sim.settle(load) == "succeeded"
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == "running")
            )
            run_id = run.id

        lifecycle = world.controller.lifecycle
        current_plan = lifecycle._stop_plan_in_session

        def always_stale(session, stopped_run_id, *, lock, **kwargs):
            # Every Stop acceptance meets records that moved on since review.
            plan = current_plan(session, stopped_run_id, lock=lock, **kwargs)
            return replace(plan, allowed=False) if lock else plan

        monkeypatch.setattr(lifecycle, "_stop_plan_in_session", always_stale)
        replacement, _number = world.create_profile("Nothing", [])
        world.controller.profiles.apply(
            replacement, request_key=str(uuid.uuid4()), actor="admin"
        )

        refusal = "stop plan is stale or blocked"
        waits: set[str] = set()
        live: list[FleetProfileApplication] = []
        for _ in range(60 * RECOVERY.max_failures):
            # The simulation's monitor is not consulted: it reports the
            # injected refusal repeating, which is the fault under test.
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()
            with world.sessions() as session:
                live = list(
                    session.scalars(
                        select(FleetProfileApplication).where(
                            FleetProfileApplication.state.not_in(ENDED)
                        )
                    )
                )
            if not live:
                break
            for row in live:
                shown = world.controller.profiles.application(row.id)
                # The wait is a named blocker that carries the refusal.
                if any(
                    item.code == PHASE_RETRY_CODE and refusal in item.detail
                    for item in shown.progress.blockers
                ):
                    waits.add(row.id)

        assert not live, "the refused Stop is still waiting"
        with world.sessions() as session:
            lineage = list(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(FleetProfileApplication.id != load)
                    .order_by(FleetProfileApplication.created_at)
                )
            )
        assert [row.state for row in lineage] == ["superseded"] * (
            RECOVERY.max_failures - 1
        ) + ["failed"]
        assert waits == {row.id for row in lineage}
        last = world.controller.profiles.application(lineage[-1].id)
        # The ending is typed and names the refusal it repeated.
        repeated = [item for item in last.progress.blockers if refusal in item.detail]
        assert PROFILE_REPEATED_FAILURE_CODE in [item.code for item in repeated]
        with world.sessions() as session:
            kept = session.get(RecipeRun, run_id)
            assert kept is not None and kept.state == "running"
    finally:
        world.close()
