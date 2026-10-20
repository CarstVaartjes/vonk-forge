"""A standing selected profile recovers when a repeated Stop conflict clears.

Wrong implementation: five identical child failures permanently end the
selected intent, so a worker restart and cleared conflict cannot finish it.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, RunState
from vonk_control.models import (
    FleetProfileApplication,
    FleetProfileSelection,
    RecipeRun,
)
from vonk_control.operation_blockers import PHASE_RETRY_CODE
from vonk_control.recovery_policy import RECOVERY

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, build_world

ROUND_SECONDS = 120


@pytest.mark.slow(30)  # a lineage of Stop children, each held for its whole window
def test_selected_stop_recovers_after_repeated_conflict_clears_and_worker_restarts(
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
        assert sim.settle(load) == LifecycleState.SUCCEEDED
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == RunState.RUNNING)
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
            with world.sessions() as session:
                applications = list(session.scalars(select(FleetProfileApplication)))
            if not live or len(applications) > RECOVERY.max_failures + 2:
                break
            for row in live:
                shown = world.controller.profiles.application(row.id)
                # The wait is a named blocker that carries the refusal.
                if any(
                    item.code == PHASE_RETRY_CODE and refusal in item.detail
                    for item in shown.progress.blockers
                ):
                    waits.add(row.id)

        assert live, "the same selected intent must keep a scheduled observation"
        assert len(applications) > RECOVERY.max_failures + 2
        assert waits, "the Stop refusal must be visible during recovery"
        with world.sessions() as session:
            selection = session.get(FleetProfileSelection, 1)
            assert selection is not None
            generation = selection.generation
            selected_id = selection.application_id
            kept = session.get(RecipeRun, run_id)
            assert kept is not None and kept.state == RunState.RUNNING

        monkeypatch.undo()
        world.restart()
        # No apply/retry/cancel request: the selected owner drives recovery.
        for _ in range(120):
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()
            with world.sessions() as session:
                kept = session.get(RecipeRun, run_id)
                completed = session.scalar(
                    select(FleetProfileApplication.id).where(
                        FleetProfileApplication.id != load,
                        FleetProfileApplication.state == LifecycleState.SUCCEEDED,
                    )
                )
                if (
                    kept is not None
                    and kept.state == RunState.STOPPED
                    and completed is not None
                ):
                    break
        with world.sessions() as session:
            kept = session.get(RecipeRun, run_id)
            assert kept is not None and kept.state == RunState.STOPPED
            selection = session.get(FleetProfileSelection, 1)
            assert selection is not None
            assert (selection.generation, selection.application_id) == (
                generation,
                selected_id,
            )
            lineage = list(
                session.scalars(
                    select(FleetProfileApplication).where(
                        FleetProfileApplication.id != load
                    )
                )
            )
            assert any(row.state == LifecycleState.SUCCEEDED for row in lineage)
            assert not any(row.state == LifecycleState.FAILED for row in lineage)
    finally:
        world.close()
