"""Installed profile intent must own and verify an install without serving."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    CatalogDocumentRevision,
    FleetProfileApplication,
    InstallationNode,
    Job,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchInstallationVerifyResult,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from cluster_profiles.control_client import validate_control_document

from .test_recipe_operations import installed_recipe, setup_services, started_recipe
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
)


def _profile_service(sessions, lifecycle):
    planner = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    return (
        build_production_fleet_profile_service(
            sessions, clock=lifecycle._clock, run_switch_operations=planner
        ),
        planner,
    )


def _installed_profile(service, sessions, nodes):
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
    return service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Prepare without serving",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": "installed",
                    }
                ],
            }
        ),
        actor="admin",
    )


def _apply(service, profile):
    review = service.preview(profile.id)
    assert review.allowed, review.reasons
    return service.apply(
        profile.id,
        plan_digest=review.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )


def _drive_to_job(service, planner, sessions, kind: str):
    for _ in range(12):
        planner.tick()
        service.tick()
        with sessions() as session:
            job = session.scalar(select(Job).where(Job.kind == kind))
            if job is not None:
                return job.id
    with sessions() as session:
        receipts = [
            (row.id, row.state, row.status_reason)
            for row in session.scalars(select(FleetProfileApplication))
        ]
        jobs = [
            (row.kind, row.state, row.status_reason)
            for row in session.scalars(select(Job))
        ]
    raise AssertionError(f"No {kind} job was created; profiles={receipts}; jobs={jobs}")


@pytest.mark.parametrize("node_count", [1, 2])
def test_installed_profile_waits_for_every_install_receipt_and_never_starts(
    tmp_path: Path, node_count: int
) -> None:
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path, nodes=node_count)
    service, planner = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    review = service.preview(profile.id)
    assert review.allowed
    application = service.apply(
        profile.id,
        plan_digest=review.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    install_id = None
    for _ in range(12):
        planner.tick()
        service.tick()
        with sessions() as session:
            install = session.scalar(select(Job).where(Job.kind == "recipe.install"))
            if install is not None:
                install_id = install.id
                break
    assert install_id is not None, service.application(application.id).status_reason
    assert service.application(application.id).state == "running"

    for index, node_id in enumerate(nodes):
        lifecycle.record_node_result(
            install_id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
        planner.tick()
        service.tick()
        if index < len(nodes) - 1:
            assert service.application(application.id).state == "running"

    # New service instances must consume the persisted installation receipt.
    restarted, restarted_planner = _profile_service(sessions, lifecycle)
    for _ in range(8):
        restarted_planner.tick()
        restarted.tick()
        if restarted.application(application.id).state == "succeeded":
            break
    completed = restarted.application(application.id)
    assert completed.state == "succeeded", completed.status_reason
    validate_control_document(
        "FleetProfileApplicationView", completed.model_dump(mode="json")
    )
    assert completed.progress.switch_adapter is not None
    assert len(completed.progress.switch_adapter.children) == 1
    child = completed.progress.switch_adapter.children[0]
    assert child.kind == "install"
    assert child.result is not None
    with sessions() as session:
        installations = tuple(session.scalars(select(RecipeInstallation)))
        assert len(installations) == 1
        assert installations[0].state == "installed"
        members = tuple(session.scalars(select(InstallationNode)))
        assert {member.node_id for member in members} == set(nodes)
        assert all(member.state == "installed" for member in members)
        assert not tuple(session.scalars(select(RecipeRun)))
        evidence = child.result.run_switch.phase_results[-1]
        assert isinstance(evidence, RunSwitchInstallationVerifyResult)
        assert evidence.final_verified
        assert evidence.installation_id == installations[0].id
        assert evidence.active_runs == 0
        assert evidence.unwithdrawn_routes == 0
        assert {rank.node_id for rank in evidence.ranks} == set(nodes)


def test_install_review_does_not_require_serving_memory_or_an_available_port(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 0
        session.add(
            ResourceReservation(
                node_id=nodes[0],
                kind="port",
                resource_key=str(run_plan.nodes[0].port),
                amount_bytes=0,
                owner_kind="test",
                owner_id=str(uuid4()),
                state="active",
                plan_digest="a" * 64,
                created_at=lifecycle._clock(),
            )
        )
    assert "run.port_occupied" in {
        reason.code
        for node in lifecycle.preview_run(installation.owner_id, "qwen").nodes
        for reason in node.blockers
    }
    _, planner = _profile_service(sessions, lifecycle)
    request = _request(sessions, nodes[0])
    assert not planner.preview(request, actor="admin").allowed
    install_request = RunSwitchApplyRequest(
        **{**request.model_dump(), "action": "install", "request_key": str(uuid4())}
    )
    review = planner.preview(install_request, actor="admin")
    assert review.allowed, review.blockers
    assert review.fit.nodes[0].memory_required_bytes == 0
    operation = planner.apply(install_request, actor="admin")
    for _ in range(4):
        planner.tick()
    assert planner.get(operation.operation_id).state == "succeeded"
    with sessions() as session:
        assert not tuple(session.scalars(select(RecipeRun)))


def test_installed_profile_still_refuses_insufficient_disk(tmp_path: Path) -> None:
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
    service, _ = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    review = service.preview(profile.id)
    assert not review.allowed
    assert "run-switch.insufficient-disk" in {
        reason.code
        for item in review.assessments
        for reason in item.assessment.blockers
    }


@pytest.mark.parametrize(
    "corruption", ["missing-member", "incomplete-member", "wrong-image"]
)
def test_successful_install_child_does_not_hide_invalid_final_installation(
    tmp_path: Path, corruption: str
) -> None:
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path, nodes=2)
    service, planner = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    application = _apply(service, profile)
    install_id = _drive_to_job(service, planner, sessions, "recipe.install")
    for node_id in nodes:
        lifecycle.record_node_result(
            install_id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    with sessions.begin() as session:
        installation = session.scalar(select(RecipeInstallation))
        member = session.scalar(select(InstallationNode))
        assert installation is not None and member is not None
        if corruption == "missing-member":
            session.delete(member)
        elif corruption == "incomplete-member":
            member.state = "planned"
        else:
            installation.image_digest = "sha256:" + "f" * 64
    for _ in range(8):
        planner.tick()
        service.tick()
    completed = service.application(application.id)
    assert completed.state == "failed"
    assert "run-switch.installation-" in (completed.status_reason or "")


def test_running_to_installed_stops_and_reuses_the_existing_installation(
    tmp_path: Path,
    postgres_engine,
) -> None:
    withdrawn: list[str] = []
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append, engine=postgres_engine
    )
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    run = started_recipe(
        sessions, lifecycle, installation.owner_id, nodes, request_id=str(uuid4())
    )
    service, planner = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    review = service.preview(profile.id)
    assert review.summary.stops == 1
    assert review.summary.installs == 0
    application = _apply(service, profile)
    stop_id = _drive_to_job(service, planner, sessions, "recipe.stop")
    lifecycle.record_node_result(
        stop_id, nodes[0], succeeded=True, evidence={"stopped": True}
    )
    for _ in range(12):
        planner.tick()
        service.tick()
    completed = service.application(application.id)
    assert completed.state == "succeeded", completed.status_reason
    assert withdrawn == [run.owner_id]
    with sessions() as session:
        assert [item.id for item in session.scalars(select(RecipeInstallation))] == [
            installation.owner_id
        ]
        runs = tuple(session.scalars(select(RecipeRun)))
        assert [item.id for item in runs] == [run.owner_id]
        assert runs[0].state == "stopped" and runs[0].route_state == "withdrawn"
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.install"))))
            == 1
        )


@pytest.mark.parametrize(
    "boundary",
    ["profile-checkpoint", "preparation-checkpoint", "installation-checkpoint"],
)
@pytest.mark.parametrize("completed_before_restart", [False, True])
def test_postgres_installed_profile_adopts_committed_child_after_crash(
    tmp_path: Path,
    postgres_engine,
    monkeypatch,
    boundary: str,
    completed_before_restart: bool,
) -> None:
    """A lost parent checkpoint cannot repeat either durable child effect."""
    engine = create_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        connect_args={"options": "-c lock_timeout=3000"},
    )
    try:
        sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path, engine=engine)
        service, planner = _profile_service(sessions, lifecycle)
        profile = _installed_profile(service, sessions, nodes)
        application = _apply(service, profile)
        with sessions() as session:
            claim_id = session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == application.id,
                    ResourceReservation.kind == "disk",
                    ResourceReservation.state == "active",
                )
            )
            assert claim_id is not None
        crashed = False
        if boundary == "profile-checkpoint":
            adapter = service._switch_adapter
            assert isinstance(adapter, RunSwitchFleetProfileAdapter)
            write_state = adapter._write_state

            def crash_after_child(session, row, state):
                nonlocal crashed
                if state.get("active_operation_id") is not None and not crashed:
                    crashed = True
                    raise SystemExit("crash after child commit")
                return write_state(session, row, state)

            monkeypatch.setattr(adapter, "_write_state", crash_after_child)
        else:
            executor = planner._phase_executor
            assert executor is not None
            execute = executor.execute

            def crash_after_install(plan, phase, **kwargs):
                nonlocal crashed
                result = execute(plan, phase, **kwargs)
                checkpoint = (
                    "runtime-plan"
                    if boundary == "preparation-checkpoint"
                    else "runtime-install"
                )
                if phase.subphase == checkpoint and not crashed:
                    crashed = True
                    raise SystemExit("crash after child commit")
                return result

            monkeypatch.setattr(executor, "execute", crash_after_install)

        with pytest.raises(SystemExit, match="crash after child commit"):
            for _ in range(12):
                planner.tick()
                service.tick()
        assert crashed

        if boundary == "preparation-checkpoint":
            # The installation and its claim committed before the parent
            # checkpoint. It must be adopted without admitting the same bytes
            # again when the next inventory has only that allocation left.
            with sessions.begin() as session:
                installation = session.scalar(select(RecipeInstallation))
                inventory = session.scalar(select(NodeInventorySnapshot))
                assert installation is not None and inventory is not None
                installed_plan = parse_stored_installation_plan(installation.plan)
                demand = installed_plan.nodes[0]
                inventory.disk_free_bytes = (
                    demand.required_bytes + demand.disk_floor_bytes
                )

        if completed_before_restart:
            # The already-authorized child can finish while its parent is down.
            for _ in range(8):
                planner.tick()
                with sessions() as session:
                    install = session.scalar(
                        select(Job).where(Job.kind == "recipe.install")
                    )
                    if install is not None:
                        break
            assert install is not None
            lifecycle.record_node_result(
                install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
            )
            for _ in range(4):
                planner.tick()

        restarted, restarted_planner = _profile_service(sessions, lifecycle)
        if not completed_before_restart:
            install_id = _drive_to_job(
                restarted, restarted_planner, sessions, "recipe.install"
            )
            lifecycle.record_node_result(
                install_id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
            )
        for _ in range(12):
            restarted_planner.tick()
            restarted.tick()
        resumed = restarted.application(application.id)
        assert resumed.state == "succeeded", resumed.status_reason
        with sessions() as session:
            assert (
                len(
                    tuple(
                        session.scalars(
                            select(Job).where(Job.kind == "recipe.run-switch.v2")
                        )
                    )
                )
                == 1
            )
            assert (
                len(
                    tuple(
                        session.scalars(select(Job).where(Job.kind == "recipe.install"))
                    )
                )
                == 1
            )
            assert len(tuple(session.scalars(select(RecipeInstallation)))) == 1
            assert not tuple(session.scalars(select(RecipeRun)))
            claim = session.get(ResourceReservation, claim_id)
            assert claim is not None and claim.owner_kind == "installation"
            assert claim.state == "active"
            assert list(
                session.scalars(
                    select(ResourceReservation.id).where(
                        ResourceReservation.owner_kind == "installation",
                        ResourceReservation.state == "active",
                        ResourceReservation.kind == "disk",
                    )
                )
            ) == [claim_id]
    finally:
        engine.dispose()
