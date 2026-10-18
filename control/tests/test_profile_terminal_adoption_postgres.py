"""A completed executor cannot pin a continuing load to vanished evidence."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, ProfileReasonCode, RunState
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileAssignmentInput,
    FleetProfileInput,
    FleetProfilePreview,
)
from vonk_control.fleet_profiles import (
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from vonk_control.models import (
    FleetProfileApplication,
    FleetProfileSelection,
    RecipeRun,
)

from .lifecycle_simulation.simulation import ROUND_SECONDS, Simulation
from .lifecycle_simulation.world import SOLO, SOLO_B, build_world


@pytest.mark.parametrize("missing_receipt", [True, False], ids=["lost-stop", "drift"])
# Real PostgreSQL worker, including the selected intent's recovery.
@pytest.mark.slow(30)
def test_terminal_owner_replans_unobservable_effects(
    tmp_path: Path, postgres_engine, missing_receipt: bool
) -> None:
    """Catches polling a succeeded owner forever after receipt loss or run drift."""
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        profiles = world.controller.profiles

        def choice(index: int, recipe: str = SOLO) -> FleetProfileAssignmentInput:
            return FleetProfileAssignmentInput(
                recipe_selector=f"vonk-forge/{world.recipes[recipe]}",
                spark_ids=[world.nodes[index]],
                assignment_name=f"lane-{index}",
            )

        def load(
            name: str, assignments: list[FleetProfileAssignmentInput]
        ) -> FleetProfileApplicationView:
            profile = profiles.create(
                FleetProfileInput(name=name, assignments=assignments), actor="admin"
            )
            return profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

        def tick() -> None:
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()

        first = load("A", [choice(0)])
        if missing_receipt:
            for _ in range(60):
                tick()
                if profiles.application(first.id).state == LifecycleState.SUCCEEDED:
                    break
            assert profiles.application(first.id).state == LifecycleState.SUCCEEDED
            owner = load("B removes A", [choice(1)])
        else:
            owner = first
        # Dispatch the owner's original queue before the successor adopts it.
        profiles.tick()
        profiles.tick()
        successor = load(
            "C continues cleanup" if missing_receipt else "A+B continues A",
            [choice(1, SOLO_B)] if missing_receipt else [choice(0), choice(1)],
        )
        with world.sessions() as session:
            plan = _persisted_profile_plan(
                session.get_one(FleetProfileApplication, successor.id)
            )
            assert isinstance(plan, FleetProfilePreview)
            assert plan.effects.adopted
            assert plan.effects.adopted[0].application_id == owner.id
            assert bool(plan.effects.adopted[0].stops) == missing_receipt
        for _ in range(60):
            tick()
            if profiles.application(owner.id).state == LifecycleState.SUCCEEDED:
                break
        assert profiles.application(owner.id).state == LifecycleState.SUCCEEDED
        assert profiles.application(successor.id).state == LifecycleState.RUNNING
        # Fault injection after success, before the next observer tick: lose the
        # durable receipt, or report that the continued run has gone away.
        with world.sessions.begin() as session:
            row = session.get_one(FleetProfileApplication, owner.id)
            progress = _persisted_profile_progress(row)
            document = progress.switch_adapter
            assert document is not None
            if missing_receipt:
                adopted_stop = plan.effects.adopted[0].stops[0]
                child = next(
                    child
                    for child in document.children
                    if child.operation_id == adopted_stop.operation_id
                )
                assert child.result is not None
                child.result = None
                row.progress = progress.model_dump(mode="json")
            else:
                run = session.scalar(
                    select(RecipeRun).where(
                        RecipeRun.alias == choice(0).assignment_name
                    )
                )
                assert run is not None
                run.state = RunState.STOPPED.value
                sim.sparks.spark(0).serving.discard(run.id)
        world.restart()
        profiles = world.controller.profiles
        # A bounded worker pass must leave the old continuing wait, with its
        # cause recorded, rather than merely increasing its retry timestamp.
        for _ in range(8):
            tick()
            ended = profiles.application(successor.id)
            if ended.state != LifecycleState.RUNNING:
                break
        assert ended.state in {LifecycleState.BACKOFF, LifecycleState.SUPERSEDED}
        assert any(b.code == ProfileReasonCode.STALE_PLAN for b in ended.blockers)
        for _ in range(60):
            tick()
            with world.sessions() as session:
                selected = profiles.application(
                    session.get_one(FleetProfileSelection, 1).application_id
                )
            if selected.state == LifecycleState.SUCCEEDED:
                break
        assert selected.state == LifecycleState.SUCCEEDED, selected.blockers
        assert selected.profile_id == successor.profile_id
        assert selected.progress.intended_profile is not None
        assert successor.progress.intended_profile is not None
        assert (
            selected.progress.intended_profile.assignments
            == successor.progress.intended_profile.assignments
        )
    finally:
        world.close()
