"""Only an active run holds ports and memory; nothing else can block a load.

Reproduces the live deadlock: a two-Spark run written under an older
contract, not running per the Controller, with no agent observations, still
held port 8888, rendezvous port 29500 and its memory on both Sparks, so a
new profile load for the same Sparks stayed Blocked forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import RecipeRun, ResourceReservation, RunNode
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
def test_old_contract_run_on_both_sparks_does_not_block_a_new_load(
    tmp_path: Path, state: str
) -> None:
    _old_contract_run_does_not_block_a_new_load(tmp_path, state)


def test_old_contract_run_does_not_block_a_new_load_on_postgres(
    tmp_path: Path, postgres_engine
) -> None:
    # The claim release takes row locks and the port index is partial: prove
    # both on the production database.
    _old_contract_run_does_not_block_a_new_load(
        tmp_path, "failed", engine=postgres_engine
    )


def _old_contract_run_does_not_block_a_new_load(
    tmp_path: Path, state: str, *, engine=None
) -> None:
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
    # The live state: an older contract's plan, not running per the
    # Controller (or "running" with a plan it can no longer read), and no
    # agent observation at all.
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.plan = {"kind": "old"}
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
    blocked = profiles.preview(profile.id)
    assert _codes(blocked) & _CLAIM_BLOCKERS, "the stale claims block the load"

    DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    ).tick()

    assert _claims(sessions, run_id) == set()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state != "running"
    review = profiles.preview(profile.id)
    assert not _codes(review) & _CLAIM_BLOCKERS
    assert review.allowed, _codes(review)


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
