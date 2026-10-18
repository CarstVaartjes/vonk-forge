"""A superseded replay ends instead of keeping a selected load waiting."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, ProfileReasonCode, SupersedeCode
from vonk_control.evidence_repeats import load_subject
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.models import (
    EvidenceRepeat,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
)

from .lifecycle_simulation.simulation import ROUND_SECONDS, Simulation
from .lifecycle_simulation.world import SOLO, SOLO_B, build_world


@pytest.mark.slow(30)  # Three overlapping loads through the PostgreSQL worker.
def test_superseded_replay_ends_and_selected_load_replans(
    tmp_path: Path, postgres_engine
) -> None:
    """Catches generic retry_conflict parking an adopted executor forever.

    A owns one lane; A+B adopts A and executes B; C+B adopts B and replaces A.
    B's executor fails transiently. Its whole-profile replay would restore A,
    so admission refuses it as superseded. That receipt must end, letting the
    selected C+B load re-plan B under its own accepted intent.
    """
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        profiles = world.controller.profiles
        choices = [
            FleetProfileAssignmentInput(
                recipe_selector=f"vonk-forge/{world.recipes[SOLO]}",
                spark_ids=[node],
                assignment_name=f"lane-{index}",
            )
            for index, node in enumerate(world.nodes)
        ]

        def load(
            name: str, assignments: list[FleetProfileAssignmentInput]
        ) -> FleetProfileApplicationView:
            profile = profiles.create(
                FleetProfileInput(name=name, assignments=assignments), actor="admin"
            )
            return profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

        first = load("A", choices[:1])
        profiles.tick()
        world.advance(1)
        older = load("A+B", choices)
        assert profiles.tick()
        assert profiles.tick()
        document = profiles.application(older.id).progress.switch_adapter
        assert document is not None and document.pending_children
        child_id = document.pending_children[0].operation_id
        world.advance(1)
        replacement = FleetProfileAssignmentInput(
            recipe_selector=f"vonk-forge/{world.recipes[SOLO_B]}",
            spark_ids=choices[0].spark_ids,
            assignment_name=choices[0].assignment_name,
        )
        newer = load("C+B", [replacement, choices[1]])
        with world.sessions() as session:
            assert profiles._adopted_application_scope(
                session, session.get_one(FleetProfileApplication, older.id)
            )
        # Inject a recoverable executor fault after the newer load adopted B.
        with world.sessions.begin() as session:
            child = session.get_one(Job, child_id)
            child.state = LifecycleState.FAILED.value
            child.status_reason = "temporary runtime dependency unavailable"
        for _ in range(8):
            profiles.tick()
            if profiles.application(older.id).state == LifecycleState.BACKOFF:
                break
        assert profiles.application(older.id).state == LifecycleState.BACKOFF

        # Reconstruct the worker over durable state before its automatic retry.
        world.restart()
        profiles = world.controller.profiles
        # The normal worker retries the accepted load; no new operator request.
        for _ in range(8):
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()
            if profiles.application(older.id).state == LifecycleState.SUPERSEDED:
                break
        ended = profiles.application(older.id)
        assert ended.state == LifecycleState.SUPERSEDED, ended.blockers
        assert ended.progress.supersede_code == SupersedeCode.SUPERSEDED_BY_INTENT
        assert ended.next_attempt_at is None
        with world.sessions() as session:
            repeats = session.scalars(
                select(EvidenceRepeat.repeats).where(
                    EvidenceRepeat.subject == load_subject(older.id),
                    EvidenceRepeat.code == ProfileReasonCode.RETRY_CONFLICT,
                )
            ).all()
        assert all(count <= 1 for count in repeats)

        for _ in range(60):
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()
            with world.sessions() as session:
                selection = session.get_one(FleetProfileSelection, 1)
                selected = profiles.application(selection.application_id)
            if selected.state == LifecycleState.SUCCEEDED:
                break
        assert selected.state == LifecycleState.SUCCEEDED, selected.blockers
        assert selected.profile_id == newer.profile_id
        assert selected.progress.intended_profile is not None
        assert newer.progress.intended_profile is not None
        assert (
            selected.progress.intended_profile.assignments
            == newer.progress.intended_profile.assignments
        )
        assert profiles.application(older.id).progress.attempt == ended.progress.attempt
        assert profiles.application(first.id).state == LifecycleState.SUPERSEDED
    finally:
        world.close()
