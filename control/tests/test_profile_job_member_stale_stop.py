"""A job member's Stop whose reviewed plan went stale is re-planned, never failed.

A later load reviews the Stop of a job member's run, and the records it reviewed
can move on before that Stop is accepted: a blocker that clears, or records that
changed. Either way the drift is the load's to re-plan: it reads the run again,
stops it, and the load succeeds.

Wrong implementation: the JobRun Stop's acceptance refused a stale plan as an
invalid request without the re-plan reason the service Stop's acceptance gives
the same drift, so the Run/Switch ended the load failed with "stop plan is stale
or blocked" (sweep 2026-10-16, trellis-2-4b on a Spark that held a job member).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.models import RecipeRun

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, build_world
from .test_artifact_jobs import _configure_artifact_recipe


@pytest.mark.parametrize("drift", ["blocked", "changed"])
def test_a_stale_job_member_stop_plan_is_re_planned_and_the_run_stops(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        member, _number = world.create_profile(
            "Job member", [world.assignment(SOLO, world.nodes[:1])]
        )
        member_load = world.controller.profiles.apply(
            member, request_key=str(uuid.uuid4()), actor="admin"
        ).id
        assert sim.settle(member_load) == "succeeded"
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == "running")
            )
            run_id = run.id

        lifecycle = world.controller.lifecycle
        current_plan = lifecycle._stop_plan_in_session
        drifted: list[str] = []

        def stale_once(session, stopped_run_id, *, lock, **kwargs):
            # The first Stop acceptance meets records that moved on since review.
            plan = current_plan(session, stopped_run_id, lock=lock, **kwargs)
            if not lock or drifted:
                return plan
            drifted.append(stopped_run_id)
            if drift == "blocked":
                return replace(plan, allowed=False)
            return replace(plan, plan_digest="0" * 64)

        monkeypatch.setattr(lifecycle, "_stop_plan_in_session", stale_once)
        replacement, _number = world.create_profile("Nothing", [])
        stop_load = world.controller.profiles.apply(
            replacement, request_key=str(uuid.uuid4()), actor="admin"
        ).id

        assert sim.settle(stop_load) == "succeeded", (
            world.controller.profiles.application(stop_load).status_reason
        )
        assert drifted == [run_id]
        with world.sessions() as session:
            stopped = session.get(RecipeRun, run_id)
            assert stopped is not None and stopped.state == "stopped"
    finally:
        world.close()
