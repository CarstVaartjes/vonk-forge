"""Only an active run holds ports and memory; nothing else can block a load.

Reproduces the live deadlock: a two-Spark run written under an older
contract, not running per the Controller, with no agent observations, still
held port 8888, rendezvous port 29500 and its memory on both Sparks, so a
new profile load for the same Sparks stayed Blocked forever.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import Job, RecipeRun, ResourceReservation, RunNode
from vonk_control.recipe_operations import prepare_exact_recipe_run_observation_nodes
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    bind_route_publications,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)

_CLAIM_BLOCKERS = {
    "run.port_occupied",
    "run.rendezvous_port_occupied",
    "run-switch.resource.insufficient_reservation_budget",
    "run-switch.resource.resident_usage_unknown",
    "run-switch.resource.insufficient_capacity",
}


def _claims(sessions, run_id: str) -> set[tuple[str, str]]:
    with sessions() as session:
        return {
            (claim.kind, claim.resource_key)
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state == "active",
                )
            )
        }


@pytest.mark.parametrize("state", ["failed", "lost", "running"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_old_contract_run_on_both_sparks_does_not_block_a_new_load(
    tmp_path: Path, state: str
) -> None:
    _old_contract_run_does_not_block_a_new_load(tmp_path, state)


@pytest.mark.usefixtures("damaged_json_rows")
def test_old_contract_run_does_not_block_a_new_load_on_postgres(
    tmp_path: Path, postgres_engine
) -> None:
    # The claim release takes row locks and the port index is partial: prove
    # both on the production database.
    _old_contract_run_does_not_block_a_new_load(
        tmp_path, "failed", engine=postgres_engine
    )


def test_a_lost_run_is_stopped_by_the_new_load_not_a_blocker(tmp_path: Path) -> None:
    sessions, profiles, profile, recovery, run_id = _load_over(
        tmp_path, state="lost", plan_readable=True
    )

    recovery.tick()

    assert _claims(sessions, run_id), "the planned Stop releases them"
    review = profiles.preview(profile.id)
    assert review.allowed, _codes(review)
    assert [stop.run_id for d in review.admission_decisions for stop in d.stops] == [
        run_id
    ]


def _old_contract_run_does_not_block_a_new_load(
    tmp_path: Path, state: str, *, engine=None
) -> None:
    sessions, profiles, profile, recovery, run_id = _load_over(
        tmp_path, state=state, plan_readable=False, engine=engine
    )
    # An unreadable run plan no longer blocks the load (its Stop is planned from the
    # saved mapping); recovery still settles the stale run and frees its claims.
    recovery.tick()

    assert _claims(sessions, run_id) == set()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "failed"
    review = profiles.preview(profile.id)
    assert not _codes(review) & _CLAIM_BLOCKERS
    assert review.allowed, _codes(review)


def _load_over(tmp_path: Path, *, state: str, plan_readable: bool, engine=None):
    """A 2-Spark run in ``state`` with no agent observation, and a new load."""

    sessions, service, queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True, engine=engine
    )
    installed = installed_recipe(service, mapping, build, nodes, request_id="i" * 36)
    started = started_recipe(
        sessions, service, installed.owner_id, nodes, request_id="r" * 36
    )
    run_id = started.owner_id
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    held = _claims(sessions, run_id)
    assert {kind for kind, _ in held} >= {"port", "unified-memory"}
    assert {key for kind, key in held if kind == "port"} >= {"29500"}
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        if not plan_readable:
            run.plan = {"kind": "old"}  # written under an older contract
        run.state = state
        for node in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            node.observed_run_generation = None
            node.observation_process_running = None
            node.observation_observed_at = None
            node.observation_endpoint_ready = None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=service,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW, run_switch_operations=run_switch
    )
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Newer load",
                "assignments": [
                    {
                        "recipe_selector": _selector(sessions),
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "newer-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )
    return sessions, profiles, profile, recovery, run_id


def _selector(sessions) -> str:
    from vonk_control.models import CatalogDocumentRevision

    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        return f"{revision.publisher}/{revision.slug}"


def _codes(review) -> set[str]:
    return {
        blocker.code
        for decision in review.admission_decisions
        for blocker in decision.blockers
    }


def _cancelled_start_run(tmp_path: Path, *, engine=None):
    """A run a cancelled start left lost, its nodes stopped, claims kept."""

    sessions, profiles, profile, recovery, run_id = _load_over(
        tmp_path, state="lost", plan_readable=True, engine=engine
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.updated_at = NOW - timedelta(seconds=60)
        for node in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            node.state = "stopped"
            node.updated_at = NOW - timedelta(seconds=60)
    return sessions, profiles, profile, recovery, run_id


def _report_empty(sessions, node_ids) -> None:
    for node_id in node_ids:
        with sessions.begin() as session:
            prepare_exact_recipe_run_observation_nodes(
                session, node_id, NOW - timedelta(seconds=1), set()
            )


def _node_ids(sessions, run_id: str) -> list[str]:
    with sessions() as session:
        return list(
            session.scalars(select(RunNode.node_id).where(RunNode.run_id == run_id))
        )


def test_a_lost_run_every_spark_reports_absent_releases_its_claims(
    tmp_path: Path,
) -> None:
    _lost_run_absence_releases_claims(tmp_path)


def test_a_lost_run_absence_releases_its_claims_on_postgres(
    tmp_path: Path, postgres_engine
) -> None:
    _lost_run_absence_releases_claims(tmp_path, engine=postgres_engine)


def _lost_run_absence_releases_claims(tmp_path: Path, *, engine=None) -> None:
    sessions, profiles, profile, recovery, run_id = _cancelled_start_run(
        tmp_path, engine=engine
    )
    nodes = _node_ids(sessions, run_id)
    assert _claims(sessions, run_id)

    # No observation yet: unknown stays unknown, the claim is held.
    recovery.tick()
    assert _claims(sessions, run_id)

    # One Spark alone is not proof for a distributed run.
    _report_empty(sessions, nodes[:1])
    recovery.tick()
    assert _claims(sessions, run_id)

    _report_empty(sessions, nodes[1:])
    recovery.tick()

    assert _claims(sessions, run_id) == set()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
    review = profiles.preview(profile.id)
    assert not _codes(review) & _CLAIM_BLOCKERS
    assert review.allowed, _codes(review)


def test_a_lost_run_still_reported_running_keeps_its_claims(tmp_path: Path) -> None:
    sessions, _, _, recovery, run_id = _cancelled_start_run(tmp_path)
    with sessions.begin() as session:
        for node in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            run = session.get(RecipeRun, run_id)
            assert run is not None
            node.observed_run_generation = run.run_generation
            node.observation_process_running = True
            node.observation_observed_at = NOW - timedelta(seconds=1)

    recovery.tick()

    assert _claims(sessions, run_id)


def test_a_run_a_live_operation_owns_is_not_settled_by_absence(
    tmp_path: Path,
) -> None:
    sessions, _, _, recovery, run_id = _cancelled_start_run(tmp_path)
    with sessions.begin() as session:
        job = session.scalar(
            select(Job).where(Job.payload["owner_id"].as_string() == run_id)
        )
        assert job is not None
        job.state = "running"

    _report_empty(sessions, _node_ids(sessions, run_id))
    recovery.tick()

    assert _claims(sessions, run_id)
    with sessions() as session:
        assert all(
            node.observation_observed_at is None
            for node in session.scalars(select(RunNode).where(RunNode.run_id == run_id))
        )
