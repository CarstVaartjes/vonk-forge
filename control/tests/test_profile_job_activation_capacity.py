"""A load's memory promise is its own run's capacity, never a competitor of it.

Production (2026-10-15, Controller 0747a7f30, the hardware sweep): loading the
hunyuan3d-omni job recipe (declared 64 GB) on spark-3542 (about 125 GB free)
retried its start until ``run-switch.final-verification-timeout``. The job
activation's admission charged the load's own 64 GB promise on top of the
64 GB it was promised for ("The demand fits once these claims are released:
fleet-profile <the load itself>"), so any job recipe declaring more than half
a Spark could never be activated. A serving start scopes the same re-plan to
its load; the job activation did not.

The real Controller services run over PostgreSQL on a fake clock with fake
Sparks (``lifecycle_simulation``). Bytes are scaled down a millionfold.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from vonk_control.models import RecipeRun, ResourceReservation

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.sparks import Memory
from .lifecycle_simulation.world import SOLO, build_world
from .test_artifact_jobs import _configure_artifact_recipe

#: spark-3542: 128 GB, about 125 GB free when idle.
_SPARK = Memory(total=12_800, idle_free=12_500)
#: hunyuan3d-omni's declared peak: fits the Spark once, not twice.
_PEAK = 6_400


def test_a_job_recipe_over_half_a_spark_is_activated_by_its_load(
    tmp_path, postgres_engine
) -> None:
    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        world.publish_successor(SOLO, peak_bytes=_PEAK)
        sim.sparks.by_node[world.nodes[0]].memory = _SPARK
        sim.round()
        profile, _number = world.create_profile(
            "Job recipe over half a Spark", [world.assignment(SOLO, world.nodes[:1])]
        )
        load = world.controller.profiles.apply(
            profile, request_key=str(uuid.uuid4()), actor="admin"
        ).id

        assert sim.settle(load) == "succeeded"
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == "running")
            )
            # The promise became the activated run's one memory claim.
            claims = session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == world.nodes[0],
                    ResourceReservation.kind == "unified-memory",
                    ResourceReservation.state.in_(("active", "promised")),
                )
            ).all()
            assert [(claim.owner_kind, claim.owner_id) for claim in claims] == [
                ("run", run.id)
            ]
    finally:
        world.close()
