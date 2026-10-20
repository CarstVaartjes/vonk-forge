"""Installed profile intent must own and verify an install without serving."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from vonk_agent_protocol import (
    LifecycleState,
    ResourceBlockerCode,
    RunState,
    RunSwitchCode,
)
from vonk_agent_protocol.agent_words import ProfileChildJobKind
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
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
    RunNode,
)
from vonk_control.platform_ports import ENDPOINT_HOST_PORTS
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchInstallationVerifyResult,
)
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.settings import (
    PROFILE_STALL_WINDOW_SECONDS,
    STORAGE_ADMISSION_RETRY_SECONDS,
)
from vonk_control.storage_demands import (
    STORAGE_EVICTING,
    STORAGE_EVICTION_TIMED_OUT,
    STORAGE_INSUFFICIENT,
    StorageDemands,
    StorageRelief,
    spark_scope,
)
from vonk_control.unused_storage_collection import (
    _STORAGE_WAIT_CODES,
    UnusedStorageCollector,
)

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
        # Tests count bytes in the thousands; the platform floor is 2 GB.
        memory_floor_bytes=0,
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
        request_key=str(uuid4()),
        actor="admin",
    )


def _tick_profile_when_due(
    service: FleetProfileService,
    planner: RunSwitchOperationService,
    lifecycle: RecipeOperationService,
    application_id: str,
) -> None:
    """Follow this accepted owner and child's durable schedules, not guessed time."""
    observed = service.application(application_id)
    now = service._clock()
    due = [observed.next_attempt_at]
    adapter = observed.progress.switch_adapter
    if adapter is not None:
        due.extend(
            planner.get(child.operation_id).next_attempt_at
            for child in adapter.pending_children
        )
    future = [instant for instant in due if instant is not None and instant > now]
    if future:
        if observed.next_attempt_at is not None and observed.next_attempt_at > now:
            # A completed native receipt cannot bypass the parent's persisted
            # observation schedule. The same step/child stays bound until due.
            service.tick()
            held = service.application(application_id)
            assert held.current_operation_id == observed.current_operation_id
            assert held.progress.step_results == observed.progress.step_results
        instant = min(future)
        for owner in (service, planner, lifecycle):
            owner._clock = lambda instant=instant: instant
    planner.tick()
    service.tick()


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
        request_key=str(uuid4()),
        actor="admin",
    )
    install_id = None
    for _ in range(12):
        _tick_profile_when_due(service, planner, lifecycle, application.id)
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
        _tick_profile_when_due(service, planner, lifecycle, application.id)
        if index < len(nodes) - 1:
            assert service.application(application.id).state == "running"

    # New service instances must consume the persisted installation receipt.
    restarted, restarted_planner = _profile_service(sessions, lifecycle)
    for _ in range(8):
        _tick_profile_when_due(restarted, restarted_planner, lifecycle, application.id)
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


def test_child_retry_preserves_plan_and_per_node_relief_through_progress_api(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches reducing node deficits/observation owners to codes at child dispatch.

    Inject a typed unknown-usage observation at the real fit producer, alongside
    measured disk loss after admission. Preview, worker/store and API must retain
    both, then the original load completes when the observations recover.
    """
    from .test_fleet_profile_api import _client, _headers

    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    service, planner = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    application = _apply(service, profile)
    collector = UnusedStorageCollector(
        sessions,
        clock=lifecycle._clock,
        lifecycle=lifecycle,
        demands=StorageDemands(lifecycle._clock),
    )
    service.bind_storage_relief(collector.relief_for_spark)
    original_fit = planner._fit
    owner = str(uuid4())
    unknown_code = f"run-switch.{ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN}"

    def unknown_usage(*args, **kwargs):
        result = original_fit(*args, **kwargs)
        _freshness, fit, blockers, _warnings, _components = result
        for node in fit.nodes:
            disk = next(
                (
                    item
                    for item in node.blockers
                    if item.code == RunSwitchCode.INSUFFICIENT_DISK
                ),
                None,
            )
            if disk is not None:
                unknown = disk.model_copy(
                    update={
                        "code": unknown_code,
                        "detail": f"Resident usage for run {owner} on {node.node_id} is unknown; "
                        "reconcile that run against fresh inventory before retrying.",
                    }
                )
                node.blockers.append(unknown)
                blockers.append(unknown)
                fit.blockers.append(unknown)
        return result

    monkeypatch.setattr(planner, "_fit", unknown_usage)
    original_preview = planner.preview

    def lose_disk_at_dispatch(*args, **kwargs):
        with sessions.begin() as session:
            for index, snapshot in enumerate(
                session.scalars(select(NodeInventorySnapshot))
            ):
                snapshot.disk_free_bytes = 11 + index
        return original_preview(*args, **kwargs)

    monkeypatch.setattr(planner, "preview", lose_disk_at_dispatch)
    assert service.tick()
    monkeypatch.setattr(planner, "preview", original_preview)
    preview = service.preview(profile.id)
    assert not preview.allowed
    expected = {
        (reason.code, tuple(reason.node_ids)): reason.detail
        for item in preview.assessments
        for reason in item.assessment.blockers
    }
    assert (unknown_code, (nodes[0],)) in expected
    assert (RunSwitchCode.INSUFFICIENT_DISK, (nodes[1],)) in expected
    relief = {}
    for node in preview.assessments[0].assessment.fit_current.nodes:
        assert node.disk_required_bytes is not None and node.disk_free_bytes is not None
        found = collector.relief_for_spark(
            node.node_id,
            node.disk_required_bytes,
            source="profile-load",
            subject=profile.id,
            reason=RunSwitchCode.INSUFFICIENT_DISK,
        )
        assert found is not None
        assert found.needed_bytes == node.disk_required_bytes - node.disk_free_bytes
        assert found.freeable_bytes == 0
        relief[node.node_id] = found, node.disk_required_bytes, node.disk_free_bytes
    for _ in range(2):
        _tick_profile_when_due(service, planner, lifecycle, application.id)
    api, codec = _client(sessions, profiles=service)
    response = api.get(
        f"/api/profile/applications/{application.id}", headers=_headers(codec)
    )
    assert response.status_code == 200, response.text
    waiting = FleetProfileApplicationView.model_validate_json(response.content)
    actual = {
        (str(item.code), tuple(item.node_ids)): item.detail for item in waiting.blockers
    }
    for identity, detail in expected.items():
        assert actual[identity] == detail
    for node_id, (found, required, available) in relief.items():
        assert actual[(found.code, (node_id,))] == found.detail
        blocker = next(
            item
            for item in waiting.blockers
            if item.code == found.code and item.node_ids == [node_id]
        )
        assert blocker.storage is not None
        assert blocker.storage.required_free_bytes == required
        assert blocker.storage.available_bytes == available
        assert blocker.storage.reclaimable_bytes == found.freeable_bytes
    assert owner in actual[(unknown_code, (nodes[0],))]
    assert waiting.next_attempt_at is not None
    with sessions() as session:
        assert not tuple(
            session.scalars(
                select(Job).where(Job.kind == ProfileChildJobKind.RUN_SWITCH)
            )
        )
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = snapshot.disk_total_bytes
    # Restart the consumer: progress and request identity are durable, and fresh
    # planning no longer sees either fault.
    restarted, restarted_planner = _profile_service(sessions, lifecycle)
    assert waiting.next_attempt_at is not None
    due_at = waiting.next_attempt_at
    for clocked in (restarted, restarted_planner, lifecycle):
        clocked._clock = lambda: due_at
    install_id = _drive_to_job(
        restarted, restarted_planner, sessions, OperationKind.RECIPE_INSTALL
    )
    for node_id in nodes:
        lifecycle.record_node_result(
            install_id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    for _ in range(12):
        _tick_profile_when_due(restarted, restarted_planner, lifecycle, application.id)
    completed = restarted.application(application.id)
    assert completed.state == LifecycleState.SUCCEEDED, completed.status_reason
    assert (
        completed.id == application.id
        and completed.request_key == application.request_key
    )
    assert completed.blockers == []
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 1


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
        # The platform picks any free authorised endpoint port, so the node is
        # only out of ports once all of them are reserved.
        assert run_plan.nodes[0].port in ENDPOINT_HOST_PORTS
        for reserved in ENDPOINT_HOST_PORTS:
            session.add(
                ResourceReservation(
                    node_id=nodes[0],
                    kind="port",
                    resource_key=str(reserved),
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


def test_a_model_the_spark_already_stores_is_not_reserved_again_by_a_load(
    tmp_path: Path,
) -> None:
    """The review's disk claim leaves out the model files an installation of the
    same model already put in the Spark's shared store (a new installation links
    them): catches a load refused, or eviction triggered, for bytes never written."""

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    _service, planner = _profile_service(sessions, lifecycle)
    request = _request(sessions, nodes[0])
    cold = planner.preview(request, actor="admin").fit.nodes[0]
    assert cold.disk_required_bytes is not None

    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    warm = planner.preview(request, actor="admin").fit.nodes[0]

    assert warm.disk_required_bytes is not None
    # The fixture's model files are 1 KiB, and nothing else changes.
    assert cold.disk_required_bytes - warm.disk_required_bytes == 1024


def test_a_load_waiting_for_disk_names_it_and_resumes_when_space_appears(
    tmp_path: Path,
) -> None:
    """A load refused for lack of disk waits (it is not failed), says how many
    bytes it needs and how many can be freed, and goes on by itself once the
    Spark has the room: catches a refusal that needs an operator, or one that
    does not say what it is waiting for."""

    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
    service, planner = _profile_service(sessions, lifecycle)
    collector = UnusedStorageCollector(
        sessions,
        clock=lifecycle._clock,
        lifecycle=lifecycle,
        demands=StorageDemands(lifecycle._clock),
    )
    service.bind_storage_relief(collector.relief_for_spark)
    profile = _installed_profile(service, sessions, nodes)

    waiting = service.apply(profile.id, request_key=str(uuid4()), actor="admin")

    assert waiting.state == "queued", waiting
    blocker = next(
        item for item in waiting.blockers if item.code == STORAGE_INSUFFICIENT
    )
    assert blocker.node_ids == [nodes[0]]
    # The disk shortage retains the accepted intent for the automatic retry.
    assert waiting.next_attempt_at is not None
    assert waiting.next_attempt_at > lifecycle._clock()

    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = snapshot.disk_total_bytes
    # Space appeared within the bound: the next retry admits the load.
    later = lifecycle._clock() + timedelta(seconds=STORAGE_ADMISSION_RETRY_SECONDS + 1)
    for clocked in (service, planner, lifecycle):
        clocked._clock = lambda: later
    _drive_to_job(service, planner, sessions, "recipe.install")
    assert service.application(waiting.id).state == "running"


def test_disk_only_review_submits_a_wait_that_reaches_spark_retention(
    tmp_path: Path, postgres_engine
) -> None:
    """Catches review refusing before an accepted owner can demand native eviction."""
    from vonk_control.spark_store_retention import store_retention_request

    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
    service, planner = _profile_service(sessions, lifecycle)
    collector = UnusedStorageCollector(
        sessions,
        clock=lifecycle._clock,
        lifecycle=lifecycle,
        demands=StorageDemands(lifecycle._clock),
    )
    service.bind_storage_relief(collector.relief_for_spark)
    profile = _installed_profile(service, sessions, nodes)
    review = service.preview(profile.id)
    assert not review.allowed
    assert review.waits_for_preparation
    assert any(
        reason.code == RunSwitchCode.INSUFFICIENT_DISK
        for assessment in review.assessments
        for reason in assessment.assessment.blockers
    )
    # Review is read-only: only accepted intent can authorize collection.
    with sessions() as session:
        before = store_retention_request(session, nodes[0], lifecycle._clock())
    assert before is not None and before.required_free_bytes == 0
    waiting = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert waiting.state == LifecycleState.QUEUED
    assert waiting.next_attempt_at is not None
    with sessions() as session:
        demand = store_retention_request(session, nodes[0], lifecycle._clock())
    assert demand is not None and demand.required_free_bytes > 0
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = snapshot.disk_total_bytes
    later = lifecycle._clock() + timedelta(seconds=STORAGE_ADMISSION_RETRY_SECONDS + 1)
    for clocked in (service, planner, lifecycle):
        clocked._clock = lambda: later
    _drive_to_job(service, planner, sessions, OperationKind.RECIPE_INSTALL)
    assert service.application(waiting.id).current_operation_id is not None


def test_disk_demand_beyond_the_device_is_not_an_eviction_wait(
    tmp_path: Path, postgres_engine
) -> None:
    """Eviction cannot grow a device: catches parking impossible disk demand."""
    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
        snapshot.disk_total_bytes = 1
    service, _planner = _profile_service(sessions, lifecycle)
    profile = _installed_profile(service, sessions, nodes)
    review = service.preview(profile.id)
    assert any(
        reason.code == RunSwitchCode.RESOURCE_DEMAND_EXCEEDS_CAPACITY
        for assessment in review.assessments
        for reason in assessment.assessment.blockers
    )
    assert not review.waits_for_preparation


def _disk_starved_load(tmp_path: Path, relief=None):
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
    service, planner = _profile_service(sessions, lifecycle)
    if relief is None:
        collector = UnusedStorageCollector(
            sessions,
            clock=lifecycle._clock,
            lifecycle=lifecycle,
            demands=StorageDemands(lifecycle._clock),
        )
        relief = collector.relief_for_spark
    service.bind_storage_relief(relief)
    profile = _installed_profile(service, sessions, nodes)
    waiting = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert waiting.state == "queued", waiting
    return sessions, lifecycle, service, planner, waiting, nodes


def _after(lifecycle, service, planner, seconds: int) -> None:
    later = lifecycle._clock() + timedelta(seconds=seconds)
    for clocked in (service, planner, lifecycle):
        clocked._clock = lambda: later
    for _ in range(4):
        planner.tick()
        service.tick()


def test_a_disk_wait_without_new_inventory_ends_then_fresh_intent_recovers(
    tmp_path: Path,
) -> None:
    """An unobserved demand cannot keep admission queued forever or poison recovery."""
    sessions, lifecycle, service, planner, waiting, nodes = _disk_starved_load(tmp_path)
    _after(lifecycle, service, planner, PROFILE_STALL_WINDOW_SECONDS + 120)
    ended = service.application(waiting.id)
    assert ended.state == LifecycleState.FAILED, ended.status_reason
    assert ended.current_operation_id is None
    assert any(
        item.code == STORAGE_EVICTION_TIMED_OUT and item.node_ids == [nodes[0]]
        for item in ended.progress.blockers
    )
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = snapshot.disk_total_bytes
        snapshot.observed_at = lifecycle._clock()
    replacement = service.apply(
        waiting.profile_id, request_key=str(uuid4()), actor="admin"
    )
    for _ in range(6):
        with sessions.begin() as session:
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            snapshot.observed_at = lifecycle._clock()
        _after(lifecycle, service, planner, STORAGE_ADMISSION_RETRY_SECONDS + 1)
        if service.application(replacement.id).current_operation_id is not None:
            break
    admitted = service.application(replacement.id)
    assert admitted.current_operation_id is not None, admitted.status_reason
    assert service.application(waiting.id).state == LifecycleState.FAILED


def test_a_load_ends_with_typed_evidence_when_reclamation_cannot_make_room(
    tmp_path: Path,
) -> None:
    """Catches the real collector's fresh insufficient answer holding admission forever."""
    from vonk_agent_protocol import StorageDemandCode

    sessions, lifecycle, service, planner, waiting, nodes = _disk_starved_load(tmp_path)
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = lifecycle._clock() + timedelta(
            seconds=PROFILE_STALL_WINDOW_SECONDS + 120
        )
    _after(lifecycle, service, planner, PROFILE_STALL_WINDOW_SECONDS + 120)
    ended = service.application(waiting.id)
    assert ended.state == LifecycleState.FAILED, ended.status_reason
    assert ended.next_attempt_at is None
    assert any(
        item.code == StorageDemandCode.INSUFFICIENT_AFTER_EVICTION
        and item.node_ids == [nodes[0]]
        and item.storage is not None
        for item in ended.progress.blockers
    )


def test_a_load_waiting_for_eviction_is_admitted_when_space_arrives(
    tmp_path: Path,
) -> None:
    """Catches ending the wait before the agent can report its reclaimed space."""

    def evicting(node_id, required, *, source, subject, reason):
        return StorageRelief(
            STORAGE_EVICTING, required, required, "Removing unused installations."
        )

    sessions, lifecycle, service, planner, waiting, _nodes = _disk_starved_load(
        tmp_path, evicting
    )
    _after(lifecycle, service, planner, PROFILE_STALL_WINDOW_SECONDS - 120)
    assert service.application(waiting.id).state == LifecycleState.QUEUED
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = snapshot.disk_total_bytes
        snapshot.observed_at = lifecycle._clock()
    _after(lifecycle, service, planner, STORAGE_ADMISSION_RETRY_SECONDS + 1)
    admitted = service.application(waiting.id)
    assert admitted.current_operation_id is not None, admitted.status_reason


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
    # The invalid installation is never accepted as a finished load.  It is an
    # unknown, not a failure: the Run/Switch observes it again at the lifecycle
    # core's backoff and says why, and the profile follows its child.
    assert service.application(application.id).state in {"queued", "running"}
    with sessions() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert child is not None
        assert child.state in {"queued", "running", LifecycleState.OBSERVING}
        assert session.scalar(select(RecipeRun)) is None


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
    lifecycle.record_node_result(stop_id, nodes[0], succeeded=True, evidence={})
    for _ in range(12):
        _tick_profile_when_due(service, planner, lifecycle, application.id)
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
                if state.pending_children and not crashed:
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

        with pytest.raises(SystemExit):
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
            _tick_profile_when_due(
                restarted, restarted_planner, lifecycle, application.id
            )
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


def _idle_installations(sessions, lifecycle, nodes, count: int, installed_bytes: int):
    """``count`` more installed-but-not-running installations on the Spark, each
    holding ``installed_bytes``, as a Spark that has run many recipes does."""

    with sessions.begin() as session:
        original = session.scalar(select(RecipeInstallation))
        assert original is not None
        member = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == original.id
            )
        )
        assert member is not None
        for index in range(count):
            copy = RecipeInstallation(
                **{
                    column.name: getattr(original, column.name)
                    for column in RecipeInstallation.__table__.columns
                }
            )
            copy.id = str(uuid4())
            copy.model_content_sha256 = f"{index + 1:064x}"
            session.add(copy)
            session.flush()
            clone = InstallationNode(
                **{
                    column.name: getattr(member, column.name)
                    for column in InstallationNode.__table__.columns
                }
            )
            clone.id = str(uuid4())
            clone.installation_id = copy.id
            clone.installed_bytes = installed_bytes
            session.add(clone)


def test_a_review_plans_the_eviction_of_many_unused_installations_instead_of_refusing(
    tmp_path: Path,
) -> None:
    """Disk that is short but coverable by unused installations is not a refusal:
    the review is allowed and says the eviction is planned. Catches the review
    that refused (``run-switch.insufficient-disk``) with hundreds of GiB of
    installed-but-not-running installations on the Spark."""

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    _, planner = _profile_service(sessions, lifecycle)
    request = _request(sessions, nodes[0])
    needed = planner.preview(request, actor="admin").fit.nodes[0].disk_required_bytes
    assert needed is not None
    _idle_installations(
        sessions, lifecycle, nodes, count=12, installed_bytes=needed * 100
    )
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0

    review = planner.preview(request, actor="admin")

    assert review.allowed, review.blockers
    after = review.fit.nodes[0].disk_free_after_bytes
    assert after is not None and after < 0

    # Genuinely unevictable: nothing unused can cover it, and the reason says why.
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
        for member in session.scalars(select(InstallationNode)):
            member.installed_bytes = 1
    refused = planner.preview(request, actor="admin")
    assert not refused.allowed
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = snapshot.disk_total_bytes
    assert planner.preview(request, actor="admin").allowed


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_review_plans_eviction_past_the_load_it_supersedes(tmp_path: Path) -> None:
    """Catches the sweep refusal (``run-switch.insufficient-disk``, 2026-10-16):
    the load being replaced still had a child in flight on another Spark, so its
    plan kept every installation on this one as a live operation and a shortfall
    the unused installations covered was refused."""

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    service, planner = _profile_service(sessions, lifecycle)
    request = _request(sessions, nodes[0])
    needed = planner.preview(request, actor="admin").fit.nodes[0].disk_required_bytes
    assert needed is not None
    _idle_installations(
        sessions, lifecycle, nodes, count=4, installed_bytes=needed * 100
    )
    profile = _installed_profile(service, sessions, nodes)
    plan = service.preview(profile.id)
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
        previous = FleetProfileApplication(
            request_key=str(uuid4()),
            profile_id=plan.profile_id,
            profile_digest=plan.profile_digest,
            plan_digest=plan.plan_digest,
            state="running",
            plan=plan.model_dump(mode="json"),
            actor="admin",
            created_at=lifecycle._clock(),
            updated_at=lifecycle._clock(),
            current_operation_id=str(uuid4()),
            progress={
                "admission_pending": False,
                "blockers": [],
                "switch_adapter": {
                    "children": [],
                    "pending_children": [
                        {"queue_index": 1, "operation_id": str(uuid4()), "kind": "run"}
                    ],
                },
            },
        )
        session.add(previous)
        session.flush()
        previous_id = previous.id

    held = planner.inspect_request(request, actor="admin")
    replacing = planner.inspect_request(
        request, actor="admin", excluded_profile_application_ids=(previous_id,)
    )

    assert [item.code for item in held.blockers] == ["run-switch.insufficient-disk"]
    assert "a live operation names it" in held.blockers[0].detail
    assert replacing.allowed, replacing.blockers


def test_a_review_plans_evicting_installations_a_saved_profile_points_to(
    tmp_path: Path,
) -> None:
    """A full Spark where only saved-profile-pointed installations are evictable
    still admits the plan, and the warning names the profile. Models stay on the
    NAS, so a later load reinstalls."""

    from vonk_control.models import CatalogDocumentRevision, FleetProfile

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    _, planner = _profile_service(sessions, lifecycle)
    request = _request(sessions, nodes[0])
    needed = planner.preview(request, actor="admin").fit.nodes[0].disk_required_bytes
    assert needed is not None
    _idle_installations(
        sessions, lifecycle, nodes, count=4, installed_bytes=needed * 100
    )
    with sessions.begin() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        session.add(
            FleetProfile(
                number=7,
                name="Pointed",
                installation_policy="keep-cached",
                assignments=[
                    {
                        "recipe_selector": f"{revision.publisher}/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "option_choices": {},
                    }
                ],
                created_by="test",
                created_at=revision.created_at,
                updated_at=revision.created_at,
            )
        )
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0

    review = planner.preview(request, actor="admin")

    assert review.allowed, review.blockers


def _fresh_after_storage_ending(sessions, service, ended):

    from .non_blocking import assert_ended_without_blocking

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        ended,
        end=lambda receipt: receipt,
        fresh=lambda _world: service.apply(
            ended.profile_id, request_key=str(uuid4()), actor="admin"
        ),
    )


def _another_recipe(sessions, installation_id: str) -> None:
    """Make an installation another recipe's, as a Spark that served a different
    model before this load holds it: no profile names it and the load cannot
    reuse it."""

    from vonk_control.models import CatalogDocument

    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        document = session.get(CatalogDocument, revision.document_id)
        assert revision is not None and document is not None
        other = CatalogDocument(
            **{
                column.name: getattr(document, column.name)
                for column in CatalogDocument.__table__.columns
            }
        )
        other.id, other.slug = str(uuid4()), "earlier-model"
        session.add(other)
        session.flush()
        copy = CatalogDocumentRevision(
            **{
                column.name: getattr(revision, column.name)
                for column in CatalogDocumentRevision.__table__.columns
            }
        )
        copy.id, copy.document_id, copy.slug = str(uuid4()), other.id, other.slug
        session.add(copy)
        session.flush()
        installation.recipe_revision_id = copy.id
        installation.model_content_sha256 = "e" * 64
        plan = parse_stored_installation_plan(installation.plan)
        installation.image_digest = "sha256:" + "e" * 64
        plan.image_digest = installation.image_digest
        for compiled in plan.compiled_execution_plans.values():
            compiled.runtime_image.image_digest = installation.image_digest
            compiled.runtime_image.oci_layout_sha256 = "e" * 64
            compiled.identity.model_artifact_set_sha256 = "e" * 64
            for artifact in compiled.artifacts:
                artifact.sha256 = "e" * 64
                artifact.model.content_sha256 = "e" * 64
        installation.plan = plan.model_dump(mode="json")
        # What it and its runs were accepted with names that recipe too.
        installation.plan = {**installation.plan, "recipe_revision_id": copy.id}
        for run in session.scalars(
            select(RecipeRun).where(RecipeRun.installation_id == installation_id)
        ):
            run.plan = {**run.plan, "recipe_revision_id": copy.id}
        for member in session.scalars(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_id
            )
        ):
            member.installed_bytes = 10**12


@pytest.mark.parametrize("sparks", [1, 2])
def test_a_started_load_waiting_for_disk_lets_a_failed_run_be_stopped_and_evicted(
    tmp_path: Path, sparks: int
) -> None:
    """Fault (production 67de1b75): a load admitted on a planned eviction
    started its switch, so it had a current operation, but its child waited for
    disk and issued nothing. Its plan then kept every installation on its Sparks
    as a live operation, the review found nothing to evict, and the load waited
    until its budget ran out. Catches that self-deadlock: the child is not
    issued while the disk is short, the waiting load renews its demand, the
    collector stops the other recipe's failed run and uninstalls it, and the
    load then installs."""

    from vonk_control.distributed_recovery.common import (
        _settle_unrecoverable,
        release_inactive_run_claims_in_session,
    )

    from .test_unused_storage_collection import _agents_succeed

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=sparks
    )
    earlier = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    ).owner_id
    run_id = started_recipe(
        sessions, lifecycle, earlier, nodes, request_id=str(uuid4())
    ).owner_id
    now = lifecycle._clock()
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        ranks = session.scalars(select(RunNode).where(RunNode.run_id == run_id)).all()
        for rank in ranks:
            rank.state = RunState.FAILED  # every rank's process died
            rank.observed_run_generation = run.run_generation  # its Spark says so
            rank.observation_process_running = False
            rank.observation_observed_at = now
        _settle_unrecoverable(run, ranks, "fault: unrecoverable", now)
        release_inactive_run_claims_in_session(session, now)
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = 0
    _another_recipe(sessions, earlier)
    service, planner = _profile_service(sessions, lifecycle)
    collector = UnusedStorageCollector(
        sessions,
        clock=lifecycle._clock,
        lifecycle=lifecycle,
        demands=StorageDemands(lifecycle._clock),
    )
    service.bind_storage_relief(collector.relief_for_spark)
    profile = _installed_profile(service, sessions, nodes)
    with sessions() as session:
        before = set(session.scalars(select(Job.id)))

    def issued(kind: str) -> Job | None:
        with sessions() as session:
            return session.scalar(
                select(Job).where(Job.kind == kind, Job.id.not_in(before))
            )

    def report(free: int) -> None:
        """A second later the Spark reports its free disk."""

        later = lifecycle._clock() + timedelta(seconds=1)
        for owner in (service, planner, lifecycle, collector):
            owner._clock = lambda later=later: later
        collector._due_at = None
        with sessions.begin() as session:
            for snapshot in session.scalars(select(NodeInventorySnapshot)):
                snapshot.observed_at = snapshot.received_at = later
                snapshot.disk_free_bytes = free

    accepted = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    # The demand admission made lapses; only the waiting load can renew it.
    collector._demands = StorageDemands(lifecycle._clock)
    # Admitted on the planned eviction, its switch starts (it has a current
    # operation) while its child waits for the disk and is never issued.
    for _ in range(4):
        _tick_profile_when_due(service, planner, lifecycle, accepted.id)
    waiting = service.application(accepted.id)
    assert waiting.current_operation_id is not None, waiting.status_reason
    assert issued("recipe.run-switch.v2") is None, waiting.status_reason
    assert waiting.progress.blockers[0].code in _STORAGE_WAIT_CODES
    assert {item.scope for item in collector._demands.active()} == {
        spark_scope(node) for node in nodes
    }

    collector.collect()

    stop = issued("recipe.stop")
    assert stop is not None, waiting.progress.blockers
    _agents_succeed(sessions, lifecycle, stop.id)
    report(0)
    assert collector.collect().installations == 1
    uninstall = issued("recipe.uninstall")
    assert uninstall is not None
    _agents_succeed(sessions, lifecycle, uninstall.id)
    report(10_000)

    # The load goes on by itself: its child is issued and installs.
    for _ in range(8):
        if issued("recipe.install") is not None:
            break
        _tick_profile_when_due(service, planner, lifecycle, accepted.id)
    assert issued("recipe.install") is not None


def test_live_copy_on_other_spark_does_not_prevent_eviction_and_load(
    postgres_engine, tmp_path: Path
) -> None:
    """A long copy on B used to pin A's unrelated installation for hours.

    Keep the old application live while the second load's actual worker waits,
    uninstalls through the lifecycle, observes disk and issues its install.
    """
    from vonk_agent_protocol import AgentOperation as OperationKind
    from vonk_control.fleet_profile_contract import (
        FleetProfileApplicationProgress,
        FleetProfilePreview,
    )

    from .test_unused_storage_collection import _agents_succeed

    sessions, lifecycle, _, mapping, build, nodes = setup_services(
        tmp_path, nodes=2, mapping_node_count=1, engine=postgres_engine
    )
    unused = installed_recipe(
        lifecycle, mapping, build, (nodes[0],), request_id=str(uuid4())
    ).owner_id
    _another_recipe(sessions, unused)
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, unused)
        assert installation is not None
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = 0 if snapshot.node_id == nodes[0] else 8000
    service, planner = _profile_service(sessions, lifecycle)
    collector = UnusedStorageCollector(
        sessions,
        clock=lifecycle._clock,
        lifecycle=lifecycle,
        demands=StorageDemands(lifecycle._clock),
    )
    service.bind_storage_relief(collector.relief_for_spark)
    profile = _installed_profile(service, sessions, (nodes[0],))
    accepted = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    for _ in range(4):
        _tick_profile_when_due(service, planner, lifecycle, accepted.id)
    waiting = service.application(accepted.id)
    assert waiting.progress.blockers[0].code == STORAGE_EVICTING
    # Fault injection: another accepted application still owns both Sparks.
    # Its pending copy on B never ends during this scenario.
    copying_id = str(uuid4())
    with sessions.begin() as session:
        current = session.get(FleetProfileApplication, accepted.id)
        assert current is not None
        copying_plan = FleetProfilePreview.model_validate_json(json.dumps(current.plan))
        copying_plan.plan_digest = "f" * 64
        assert set(copying_plan.scope.node_ids) == set(nodes)
        copying = FleetProfileApplication(
            id=copying_id,
            request_key=str(uuid4()),
            profile_id=current.profile_id,
            profile_digest=current.profile_digest,
            plan_digest=copying_plan.plan_digest,
            plan=copying_plan.model_dump(mode="json"),
            state=LifecycleState.RUNNING,
            actor="admin",
            current_operation_id=copying_id,
            next_action_at=lifecycle._clock() + timedelta(hours=2),
            progress=FleetProfileApplicationProgress().model_dump(mode="json"),
            created_at=lifecycle._clock(),
            updated_at=lifecycle._clock(),
        )
        session.add(copying)
    assert collector.collect().installations == 1
    with sessions() as session:
        uninstall = session.scalar(
            select(Job).where(Job.kind == OperationKind.RECIPE_UNINSTALL.value)
        )
        assert uninstall is not None
    _agents_succeed(sessions, lifecycle, uninstall.id)
    later = lifecycle._clock() + timedelta(seconds=1)
    for owner in (service, planner, lifecycle, collector):
        owner._clock = lambda: later
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.observed_at = snapshot.received_at = later
            snapshot.disk_free_bytes = 10000
    for _ in range(8):
        _tick_profile_when_due(service, planner, lifecycle, accepted.id)
        with sessions() as session:
            install = session.scalar(
                select(Job).where(
                    Job.kind == OperationKind.RECIPE_INSTALL.value,
                    Job.created_at >= later,
                )
            )
        if install is not None:
            break
    assert install is not None
    with sessions() as session:
        copying = session.get(FleetProfileApplication, copying_id)
        assert copying is not None and copying.state == LifecycleState.RUNNING


def test_prepared_installation_is_referenced_before_its_child_is_issued(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches replacing the node hold while ignoring the prepared result."""
    from vonk_agent_protocol.agent_words import ProfileChildJobKind, ProfileChildPhase
    from vonk_control.catalog_revision_collection import operation_tokens
    from vonk_control.job_documents import RunSwitchJobPayload
    from vonk_control.run_switch_contract import (
        RunSwitchOperationResult,
        RunSwitchRuntimeInstallResult,
    )

    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    _, planner = _profile_service(sessions, lifecycle)
    plan = planner.preview(_request(sessions, nodes[0]), actor="admin")
    prepared_id = str(uuid4())
    payload = RunSwitchJobPayload(
        schema_version=2,
        operation_kind=ProfileChildJobKind.RUN_SWITCH.value,
        action=plan.action,
        plan_digest=plan.plan_digest,
        plan=plan,
        progress=RunSwitchOperationResult(),
    )
    result = RunSwitchOperationResult(
        phase_results=[
            RunSwitchRuntimeInstallResult(
                phase=ProfileChildPhase.PREPARE.value,
                subphase=ProfileChildPhase.RUNTIME_INSTALL.value,
                installation_id=prepared_id,
            )
        ]
    )
    with sessions.begin() as session:
        session.add(
            Job(
                request_id=str(uuid4()),
                kind=ProfileChildJobKind.RUN_SWITCH.value,
                state=LifecycleState.RUNNING,
                actor="admin",
                authority_revision="r",
                targets=list(nodes),
                payload_digest=plan.plan_digest,
                payload=payload.model_dump(mode="json"),
                result=result.model_dump(mode="json"),
                current_attempt=1,
                created_at=lifecycle._clock(),
                updated_at=lifecycle._clock(),
            )
        )
    with sessions() as session:
        assert prepared_id in operation_tokens(
            session, lifecycle._clock(), recent=False
        )
