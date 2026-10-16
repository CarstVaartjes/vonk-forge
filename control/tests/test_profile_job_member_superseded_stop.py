"""A job member's Stop superseded before acceptance ends superseded, never failed.

A later load reviews the Stop of a job member's one-shot run, and a newer
workload intent can reach the run's Spark before that Stop is accepted: while
the load retires older unissued work, or under the Stop's own lock. Nothing was
issued: the Stop goes back to its Run/Switch, which reads the newer intent and
ends ``cancelled`` as superseded, and the run is left to the newer intent.

Wrong implementation: these checks refused the newer intent as an untyped
invalid request, which Run/Switch does not re-plan, so the Stop ended failed
with "workload intent was superseded".
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import AgentNode, Job, RecipeRun, RunNode
from vonk_control.recipe_operations import job_run_stop

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.simulation import ROUND_SECONDS, Simulation
from .lifecycle_simulation.world import SOLO, build_world
from .test_artifact_jobs import _configure_artifact_recipe


@pytest.mark.slow(30)  # a whole member load, then its Stop load, on PostgreSQL
@pytest.mark.parametrize("moment", ["retire-unissued", "accept"])
def test_a_job_member_stop_superseded_before_acceptance_is_not_failed(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch, moment: str
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
            node_ids = tuple(
                session.scalars(select(RunNode.node_id).where(RunNode.run_id == run_id))
            )
        superseded: list[int] = []

        def newer_intent() -> None:
            # A newer workload intent reaches the run's Spark, as a newer
            # load's admission does, after the Stop was reviewed.
            if superseded:
                return
            with world.sessions.begin() as session:
                nodes = tuple(
                    session.scalars(
                        select(AgentNode).where(AgentNode.node_id.in_(node_ids))
                    )
                )
                ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
                for node in nodes:
                    node.workload_intent_ordinal = ordinal
                AgentJobService.request_superseded_workload_cancellation_in_session(
                    session, node_ids, ordinal, world.clock()
                )
            superseded.append(ordinal)

        lifecycle = world.controller.lifecycle
        if moment == "retire-unissued":
            retire = lifecycle.reconcile_superseded_unissued

            def retire_after_newer_intent(kind, owner_id, ordinal, **kwargs):
                if kind == "recipe.stop":
                    newer_intent()
                return retire(kind, owner_id, ordinal, **kwargs)

            monkeypatch.setattr(
                lifecycle, "reconcile_superseded_unissued", retire_after_newer_intent
            )
        else:
            one_shot = job_run_stop._run_is_one_shot
            checks: list[str] = []

            def newer_intent_under_lock(session, run: RecipeRun) -> bool:
                # The Stop's acceptance reads the run before its lock and again
                # under it; the newer intent lands between the two.
                checks.append(run.id)
                if len(checks) == 2:
                    newer_intent()
                return one_shot(session, run)

            monkeypatch.setattr(
                job_run_stop, "_run_is_one_shot", newer_intent_under_lock
            )

        replacement, _number = world.create_profile("Nothing", [])
        stop_load = world.controller.profiles.apply(
            replacement, request_key=str(uuid.uuid4()), actor="admin"
        ).id
        profiles = world.controller.profiles
        # Rounds without the monitor: no newer load exists to own the injected
        # intent, which the monitor would report as a load superseded by nothing.
        for _ in range(40):
            if profiles.application(stop_load).state in ENDED:
                break
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()

        assert superseded, "the newer intent never reached the Stop"
        assert profiles.application(stop_load).state == "superseded"
        with world.sessions() as session:
            (stop,) = session.scalars(select(Job).where(Job.kind == "recipe.stop.v2"))
            assert stop.state == "cancelled", stop.status_reason
            run_after = session.get(RecipeRun, run_id)
            assert run_after is not None and run_after.state == "running"
    finally:
        world.close()
