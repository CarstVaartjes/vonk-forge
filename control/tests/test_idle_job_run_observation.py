"""An idle job member survives its Spark's complete empty run reports.

A job recipe's run is a held reservation: nothing runs between submissions,
and each job is a one-shot container of its own. So a Spark whose only
workload is a job member truthfully reports, every sweep, that it runs no
recipe run. That report is not evidence the job run is lost: the run has no
process to report. The run stays the member's, with no re-activation.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from vonk_control.agent_jobs import AgentJobService
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.models import FleetProfileApplication, Job, RecipeRun, RunNode
from vonk_control.recipe_operations import prepare_exact_recipe_run_observation_nodes
from vonk_control.recipe_routes import RecipeRouteService

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, build_world
from .test_artifact_jobs import _configure_artifact_recipe
from .test_profile_job_member import _ACTIVATION, _load


def test_an_idle_job_member_stays_running_across_complete_empty_reports(
    tmp_path: Path, postgres_engine
) -> None:
    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        spark = world.nodes[:1]
        member, _number = world.create_profile(
            "Job member", [world.assignment(SOLO, spark)]
        )
        assert sim.settle(_load(world, member)) == "succeeded"
        with world.sessions() as session:
            (held,) = session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
            loads = session.scalar(
                select(func.count()).select_from(FleetProfileApplication)
            )
        routes = world.controller.worker._routes
        assert isinstance(routes, RecipeRouteService)
        recoveries = DistributedRecoveryCoordinator(
            world.sessions,
            routes=routes,
            agent_jobs=AgentJobService(world.sessions, clock=world.clock),
            clock=world.clock,
            recovery_run_stops=world.controller.lifecycle,
        )

        # The Spark's agent reports, completely, that it runs nothing: every
        # sweep, as the real agent does while the member waits for a job.
        for _ in range(15):
            world.advance(30)
            with world.sessions.begin() as session:
                prepare_exact_recipe_run_observation_nodes(
                    session, spark[0], world.clock(), set()
                )
            recoveries.tick()
            sim.round()

        with world.sessions() as session:
            run = session.get(RecipeRun, held)
            assert run is not None and run.state == "running"
            assert set(
                session.scalars(select(RunNode.state).where(RunNode.run_id == held))
            ) == {"running"}
            assert session.scalars(
                select(Job.payload["owner_id"].as_string()).where(
                    Job.kind == _ACTIVATION
                )
            ).all() == [held]
            assert (
                session.scalar(
                    select(func.count()).select_from(FleetProfileApplication)
                )
                == loads
            )
            assert session.scalar(select(Job.id).where(Job.kind == "recipe.stop")) is (
                None
            )
        assert world.controller.profiles.get(member).assignments[0].observed_state == (
            "Running"
        )
        assert world.controller.profiles._reconcile_selected_roster(world.clock()) is (
            False
        )
    finally:
        world.close()
