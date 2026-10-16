"""A job member's Stop whose run changed before acceptance is re-planned.

A later load reviews the Stop of a job member's one-shot run, and the run row
can move on before that Stop is accepted under its lock (it is gone, or no
longer reads as a one-shot run). That is drift in the Controller's own records,
not a request that changed: the load re-plans, reads the run again, stops it,
and succeeds.

Wrong implementation: the JobRun Stop's acceptance refused the changed run as a
conflict, which Run/Switch does not re-plan, so the load ended failed with
"logical recipe run changed while stopping".
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.models import RecipeRun
from vonk_control.recipe_operations import job_run_stop

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, build_world
from .test_artifact_jobs import _configure_artifact_recipe


def test_a_job_member_whose_run_changed_before_its_stop_is_re_planned(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
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

        one_shot = job_run_stop._run_is_one_shot
        checks: list[str] = []
        drifted: list[str] = []

        def changed_once(session, run: RecipeRun) -> bool:
            # The Stop's first acceptance reads the run before its lock and
            # again under it; between the two, the run's records moved on.
            checks.append(run.id)
            if len(checks) == 2 and not drifted:
                drifted.append(run.id)
                return False
            return one_shot(session, run)

        monkeypatch.setattr(job_run_stop, "_run_is_one_shot", changed_once)
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
