"""A profile loaded over an identical run under another alias is not blocked.

Production (2026-11-06, the hardware sweep's restore of the owner's profile):
the owner's assignment equalled the sweep's running GLM run except for the
route alias.  The planner kept that run (identity ignores the alias), while
admission charged a full second start beside it, so the load waited on the
very run its own plan kept, and when it ended the owner's alias stayed
withdrawn because routes are named by the run's alias.

One predicate decides which run serves an assignment: the installation's run
under the assignment's alias.  A run under another alias is replaced, not kept.

The real Controller services run over PostgreSQL on a fake clock with fake
Sparks (``lifecycle_simulation``).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from vonk_control.models import RecipeRun

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, World, build_world

_BLOCKERS = {
    "run.port_occupied",
    "run.rendezvous_port_occupied",
    "run.insufficient_memory",
    "run.insufficient_capacity",
    "run-switch.run_admission_blocked",
}


def _load(world: World, profile_id: str) -> str:
    return world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _profile(world: World, name: str, alias: str) -> tuple[str, int]:
    assignment = world.assignment(SOLO, world.nodes[:1])
    assignment["assignment_name"] = alias
    return world.create_profile(name, [assignment])


def _serving_aliases(world: World) -> list[str]:
    with world.sessions() as session:
        return sorted(
            session.scalars(select(RecipeRun.alias).where(RecipeRun.state == "running"))
        )


def _endpoint_runs(world: World, number: int) -> list[tuple[str | None, str | None]]:
    """(alias, run id) of each assignment whose endpoint a run will publish."""

    with world.sessions() as session:
        intent = world.controller.profiles.endpoint_intent(session, number)
    assert intent.assignments is not None
    return [(item.alias, item.expected_run_id) for item in intent.assignments]


@pytest.mark.slow(30)
def test_a_profile_over_an_identical_run_under_another_alias_replaces_it(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        first, _first_number = _profile(world, "Sweep", "sweep-alias")
        second, second_number = _profile(world, "Owner", "owner-alias")
        assert sim.settle(_load(world, first)) == "succeeded"
        assert _serving_aliases(world) == ["sweep-alias"]

        review = world.controller.profiles.preview(second)
        # No blocker names the run the load replaces, and nothing is kept.
        assert review.allowed
        assert not _BLOCKERS & {
            reason.code
            for item in review.assessments
            for reason in item.assessment.blockers
        }
        assert all("keep" not in item.actions for item in review.assignments)

        load = _load(world, second)
        # Liveness: a load never waits on a run its own plan keeps.
        assert sim.settle(load) == "succeeded"
        assert _serving_aliases(world) == ["owner-alias"]
        sim.round()
        with world.sessions() as session:
            (run_id,) = session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
        # The endpoint is the new run, under the profile's alias.
        assert _endpoint_runs(world, second_number) == [("owner-alias", run_id)]

        # The same alias again is a plain keep: allowed, nothing to do.
        again = world.controller.profiles.preview(second)
        assert again.allowed
        assert [item.actions for item in again.assignments] == [["keep"]]
    finally:
        world.close()
