"""Revocation fences new effects while accepted routes and issued receipts survive."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    InstallationState,
    RecipeStopResult,
    RecipeUninstallResult,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.fleet_profile_contract import FleetProfileInput, ProfileReasonCode
from vonk_control.fleet_profiles import (
    _persisted_profile_progress,
    build_production_fleet_profile_service,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    CatalogDocumentRevision,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    User,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
from vonk_control.route_runtime import AtomicRouteBundlePublisher
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.terminal_history_collection import TerminalHistoryCollector

from .preflight_fixtures import record_passing_preflight
from .runtime_identity_support import claim_agent
from .test_fleet_profiles import _node_id, _uuid
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)
from .test_terminal_history_collection import _job


def _selection(sessions):
    with sessions() as session:
        row = session.get(FleetProfileSelection, 1)
        assert row is not None
        return row.generation, row.profile_id, row.application_id, row.roster_digest


def _journal(sessions, application_id):
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        progress = _persisted_profile_progress(row)
        assert progress.switch_adapter is not None
        return progress.switch_adapter


def _holds(sessions, owner_ids):
    with sessions() as session:
        return {
            (row.id, row.owner_kind, row.owner_id, row.node_id, row.state)
            for row in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id.in_(owner_ids)
                )
            )
        }


def _follow_due(sessions, worker, application_id, clock):
    """Early observation preserves the accepted child; only its due clock advances."""
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        progress = _persisted_profile_progress(row)
        due = progress.retry_due_at
        if due is None or due <= clock[0]:
            return None
        identity = (row.id, row.request_key, row.plan_digest, row.current_operation_id)
        journal = progress.switch_adapter
        assert journal is not None
        pending = list(journal.pending_children)
        closed = list(journal.children)
    # Other coordinators may progress, but this not-yet-due profile cannot reissue.
    worker.tick()
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        assert (
            row.id,
            row.request_key,
            row.plan_digest,
            row.current_operation_id,
        ) == identity
        progress = _persisted_profile_progress(row)
        assert progress.retry_due_at == due
        journal = progress.switch_adapter
        assert journal is not None
        assert journal.pending_children == pending and journal.children == closed
    clock[0] = due
    return due.isoformat()


@pytest.mark.parametrize("authority_change", ["disabled", "demoted"])
def test_serving_selection_observes_issued_stop_but_fences_new_cleanup_until_author_returns(
    postgres_engine, tmp_path, authority_change
):
    """Catches withdrawing A, losing B's receipt, admitting cleanup or changing intent."""
    sessions, lifecycle, _queue, mapping_a, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installed_a = installed_recipe(
        lifecycle, mapping_a, build_id, nodes, request_id=_uuid(19300)
    )
    run_a = started_recipe(
        sessions,
        lifecycle,
        installed_a.owner_id,
        nodes,
        request_id=_uuid(19301),
        alias="retained-a",
    )
    node_b = _node_id(2)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_b,
                state="active",
                architecture="linux-arm64",
                protocol_version=2,
                last_seen_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentCertificate(
                serial="serial-b",
                node_id=node_b,
                fingerprint="fingerprint-b",
                not_before=NOW,
                not_after=NOW + timedelta(days=1),
            )
        )
        session.add(
            AgentPresence(
                node_id=node_b,
                certificate_serial="serial-b",
                certificate_fingerprint="fingerprint-b",
                management_address="192.168.1.212",
                observed_at=NOW,
            )
        )
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        revision_id, selector = revision.id, f"{revision.publisher}/{revision.slug}"
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            node_b,
            NOW,
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("runtime.vonk.v1", "recipe.image.pull.v1", "recipe.operations.v1"),
            memory_pool="shared",
        )
    )
    record_passing_preflight(sessions, NOW)
    mappings = ClusterMappingService(sessions)
    mapping_b = mappings.materialize(
        mappings.preview(revision_id, (node_b,), {}, "admin"), actor="admin", now=NOW
    )
    installed_b = installed_recipe(
        lifecycle, mapping_b, build_id, (node_b,), request_id=_uuid(19302)
    )
    run_b = started_recipe(
        sessions,
        lifecycle,
        installed_b.owner_id,
        (node_b,),
        request_id=_uuid(19303),
        alias="retired-b",
    )
    clock = [NOW]
    lifecycle._clock = lambda: clock[0]
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    jobs.set_result_consumer(lifecycle.consume_agent_result)
    lifecycle._agent_jobs = jobs
    coordinator = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: clock[0],
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: clock[0], run_switch_operations=coordinator
    )
    runtime = AtomicRouteBundlePublisher(tmp_path / "routes")
    publisher = AtomicRecipeRoutePublisher(runtime)
    policy = ManagementAddressPolicy.parse("192.168.1.0/24")
    routes = RecipeRouteService(
        sessions, publisher=publisher, management_policy=policy, clock=lambda: clock[0]
    )

    def withdraw(run_id: str) -> None:
        routes.withdraw_run(run_id)

    lifecycle._route_withdrawer = withdraw
    routes.publish_run(run_a.owner_id)
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Keep serving A and retire B",
                "installation_policy": "exact",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "retained-a",
                    }
                ],
            }
        ),
        actor="admin",
    )
    accepted = profiles.apply(profile.id, request_key=_uuid(19304), actor="admin")
    selection = _selection(sessions)
    history = _job(NOW - timedelta(days=2))
    with sessions.begin() as session:
        session.add(history)
    worker = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: clock[0],
        fleet_profiles=profiles,
        run_switches=coordinator,
        recoveries=TerminalHistoryCollector(sessions, clock=lambda: clock[0]),
    )
    # Drive only native coordinators until B's exact Stop has been issued.
    stop_claim = None
    due_checks = []
    for _ in range(16):
        due = _follow_due(sessions, worker, accepted.id, clock)
        if due is not None:
            due_checks.append(due)
        worker.tick()
        stop_claim = claim_agent(jobs, node_b, "serial-b")
        if stop_claim is not None:
            break
    assert stop_claim is not None and stop_claim.operation.value == "recipe.stop"
    before = _journal(sessions, accepted.id)
    with sessions() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        accepted_identity = (row.id, row.request_key, row.plan_digest)
    assert {item.kind for item in before.queue} == {"stop", "cleanup"}
    accepted_a = publisher.accepted_run(run_a.owner_id, policy)
    assert accepted_a is not None
    hold_owners = (
        accepted.id,
        run_a.owner_id,
        run_b.owner_id,
        installed_a.owner_id,
        installed_b.owner_id,
    )
    holds = _holds(sessions, hold_owners)
    assert holds
    with sessions() as session:
        issued_ids = set(session.scalars(select(AgentOperation.id)))
    # A second old row becomes due during the revoked-author worker pass.
    sibling_history = _job(NOW - timedelta(days=2))
    with sessions.begin() as session:
        session.add(sibling_history)
        author = session.scalar(select(User).where(User.subject == "admin"))
        assert author is not None
        if authority_change == "disabled":
            author.disabled_at = NOW
        else:
            author.role = "viewer"
    sibling = TerminalHistoryCollector(sessions, clock=lambda: clock[0])
    worker = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: clock[0],
        fleet_profiles=profiles,
        run_switches=coordinator,
        recoveries=sibling,
    )
    assert worker.tick()
    assert _selection(sessions) == selection
    assert publisher.accepted_run(run_a.owner_id, policy) == accepted_a
    assert _holds(sessions, hold_owners) == holds
    with sessions() as session:
        assert session.get(Job, sibling_history.id) is None
        assert set(session.scalars(select(AgentOperation.id))) == issued_ids
        serving = session.get(RecipeRun, run_a.owner_id)
        assert serving is not None and serving.state == "running"
    # The issued exact receipt still belongs to B while new child admission is denied.
    jobs.record_result(
        AgentResult(
            fence=stop_claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )
    for _ in range(16):
        due = _follow_due(sessions, worker, accepted.id, clock)
        if due is not None:
            due_checks.append(due)
        worker.tick()
        current = profiles.application(accepted.id)
        if any(
            b.code == ProfileReasonCode.SWITCH_AUTHORITY_UNAVAILABLE
            for b in current.blockers
        ):
            break
    assert any(
        b.code == ProfileReasonCode.SWITCH_AUTHORITY_UNAVAILABLE
        for b in current.blockers
    )
    blocked = _journal(sessions, accepted.id)
    assert any(c.state == "succeeded" for c in blocked.children)
    assert blocked.request_id == before.request_id and blocked.queue == before.queue
    assert _selection(sessions) == selection
    assert publisher.accepted_run(run_a.owner_id, policy) == accepted_a
    with sessions() as session:
        stopped = session.get(RecipeRun, run_b.owner_id)
        cached = session.get(RecipeInstallation, installed_b.owner_id)
        assert stopped is not None and stopped.state == "stopped"
        assert cached is not None and cached.state == "installed"
        assert set(session.scalars(select(AgentOperation.id))) == issued_ids
    with sessions.begin() as session:
        author = session.scalar(select(User).where(User.subject == "admin"))
        assert author is not None
        author.disabled_at = None
        author.role = "administrator"
    # The stored retry deadline, not another load or changed plan, resumes cleanup.
    assert current.next_attempt_at is not None
    for _ in range(16):
        due = _follow_due(sessions, worker, accepted.id, clock)
        if due is not None:
            due_checks.append(due)
        worker.tick()
        claim = claim_agent(jobs, node_b, "serial-b")
        if claim is not None:
            assert claim.operation.value == "recipe.uninstall"
            jobs.record_result(
                AgentResult(
                    fence=claim.fence,
                    state=AgentResultState.SUCCEEDED,
                    result=RecipeUninstallResult(),
                )
            )
        if profiles.application(accepted.id).state == "succeeded":
            break
    assert profiles.application(accepted.id).state == "succeeded"
    assert _selection(sessions) == selection
    assert _journal(sessions, accepted.id).queue == before.queue
    assert publisher.accepted_run(run_a.owner_id, policy) == accepted_a
    with sessions() as session:
        serving = session.get(RecipeRun, run_a.owner_id)
        assert serving is not None and serving.state == "running"
        assert set(session.scalars(select(RecipeRun.id))) == {
            run_a.owner_id,
            run_b.owner_id,
        }
        removed = session.get(RecipeInstallation, installed_b.owner_id)
        assert removed is not None and removed.state == InstallationState.UNINSTALLED

        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        assert (row.id, row.request_key, row.plan_digest) == accepted_identity
    assert due_checks, "proof must actually reconnect across persisted retry deadlines"
    if output := os.environ.get("VONK_AUTHOR_CONTINUITY_PROOF_OUTPUT"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{authority_change}.json").write_text(
            json.dumps(
                {
                    "source_sha": os.environ["VONK_PROOF_SOURCE_SHA"],
                    "authority_change": authority_change,
                    "application_id": accepted.id,
                    "request_key": accepted_identity[1],
                    "plan_digest": accepted_identity[2],
                    "stop_fence": stop_claim.fence,
                    "selection": selection,
                    "persisted_due_checks": due_checks,
                    "same_intent_succeeded": True,
                    "retained_run_id": run_a.owner_id,
                    "retained_route_unchanged": True,
                    "retired_run_id": run_b.owner_id,
                    "closed_queue_indices": [
                        c.queue_index for c in _journal(sessions, accepted.id).children
                    ],
                },
                indent=2,
            )
            + "\n"
        )
