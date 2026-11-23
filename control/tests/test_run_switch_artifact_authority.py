from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from vonk_agent_protocol import (
    LibraryAssessmentCode,
    LifecycleState,
    ModelCacheBlockerCode,
    RunSwitchCode,
    canonical_message,
)
from vonk_control.disk_reservations import runtime_images_named_on_nodes
from vonk_control.inventory.repository import (
    INVENTORY_MAX_AGE,
    InventoryRepository,
    NodeInventoryReport,
    spark_model_objects,
)
from vonk_control.library.assessment import LibraryAssessment
from vonk_control.library.contract import RecipeReadinessCheck
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.jobs import Job
from vonk_control.run_switch_contract.evidence import RunSwitchReason
from vonk_control.run_switch_contract.requests import RunSwitchApplyRequest
from vonk_control.run_switch_operations.artifact_inspection import (
    DatabaseRunSwitchArtifactInspector,
    failed_copies_read_once,
    missing_spark_bytes_by_node,
)
from vonk_control.run_switch_operations.planning_helpers import _stored_job_plan
from vonk_control.run_switch_operations.result_helpers import (
    _persisted_result,
    _read_progress,
)
from vonk_control.run_switch_operations.service import RunSwitchOperationService

from .run_switch_operations_support import (
    ModelCacheManifestProvider,
    RecordingArtifactExecutor,
    _request,
)
from .runtime_image_fixtures import hold_image_through_another_recipe
from .test_recipe_operations import NOW, installed_recipe, setup_services


def test_missing_artifact_authority_does_not_poison_repaired_inspection(
    tmp_path,
) -> None:
    inspector = DatabaseRunSwitchArtifactInspector()

    # No provider is a blocker the plan observes again, never an exception.
    inspection = inspector.inspect(
        None,  # type: ignore[arg-type]
        model_content_sha256="a" * 64,
        recipe_revision_id="recipe-revision",
        node_ids=("spk_" + "b" * 32,),
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert inspection.required_bytes is None
    assert inspection.missing_nas_bytes is None
    assert inspection.missing_spark_bytes is None
    assert inspection.artifact_set_sha256 is None
    sessions, _lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    inspector.bind_model_cache(provider)
    with sessions() as session:
        repaired = inspector.inspect(
            session,
            model_content_sha256="a" * 64,
            recipe_revision_id="recipe-revision",
            node_ids=(nodes[0],),
            now=datetime(2026, 9, 6, tzinfo=UTC),
        )
    assert not repaired.blockers
    assert (
        repaired.artifact_set_sha256
        == provider.resolve_artifact_set(model_content_sha256="a" * 64).digest
    )
    assert repaired.missing_nas_bytes == 0
    assert repaired.missing_spark_bytes == repaired.artifact_set_bytes
    assert repaired.required_bytes == repaired.artifact_set_bytes


def _report_store(
    sessions,
    node_id: str,
    objects,
    *,
    received: datetime = NOW,
    observed: timedelta = timedelta(seconds=1),
) -> None:
    """The agent's next inventory, ingested like any other, with its store scan."""

    InventoryRepository(sessions, clock=lambda: received).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": NOW + observed,
                    "disk_total_bytes": 10_000,
                    "disk_free_bytes": 8_000,
                    "host_memory_total_bytes": 10_000,
                    "host_memory_free_bytes": 8_000,
                    "gpu_memory_total_bytes": 10_000,
                    "gpu_memory_free_bytes": 8_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": (
                        "runtime.vonk.v1",
                        "recipe.image.pull.v1",
                        "recipe.operations.v1",
                    ),
                    "memory_pool": "shared",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                    "model_objects": objects,
                }
            )
        )
    )


@pytest.mark.parametrize("scan", ["held", "empty", "unknown"])
def test_rerun_of_an_installed_workload_copies_only_what_the_spark_lacks(
    tmp_path, scan: str
) -> None:
    """Spark coverage is the agent's own store scan: a Spark that reports the
    model's files needs no target copy, so a re-run plans none. One
    that lacks them, or reports no scan, gets the copy, which the agent dedups."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    request = _request(sessions, nodes[0])
    manifest = provider.resolve_artifact_set(
        model_content_sha256=request.model_content_sha256
    )
    held = scan == "held"
    _report_store(
        sessions,
        nodes[0],
        None
        if scan == "unknown"
        else [
            {"sha256": spec.sha256, "bytes": spec.expected_bytes}
            for spec in manifest.artifacts
        ]
        if held
        else [],
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=provider,
        build_archive_available=lambda _digest, _size: True,
        memory_floor_bytes=50,
    )

    plan = service.preview(request, actor="admin")

    assert plan.allowed, [reason.code for reason in plan.blockers]
    copies = [phase for phase in plan.phases if phase.subphase == "target-copy"]
    missing = 0 if held else manifest.expected_bytes
    assert plan.storage.missing_spark_bytes_by_node == {nodes[0]: missing}
    assert bool(copies) is not held
    if held:
        with sessions() as session:
            # The same scan, once older than the freshness window, says nothing.
            assert spark_model_objects(session, nodes[0], now=NOW)
            stale = NOW + INVENTORY_MAX_AGE + timedelta(seconds=1)
            assert spark_model_objects(session, nodes[0], now=stale) is None


def test_replan_after_a_failed_target_copy_waits_for_a_scan_received_after_it(
    tmp_path,
) -> None:
    """A copy that ended definite re-plans the copy while the only scan that
    lists the files pre-dates it; a scan received after it settles the plan."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    request = _request(sessions, nodes[0])
    manifest = provider.resolve_artifact_set(
        model_content_sha256=request.model_content_sha256
    )
    held = [
        {"sha256": spec.sha256, "bytes": spec.expected_bytes}
        for spec in manifest.artifacts
    ]
    first_scan = NOW - timedelta(minutes=2)
    failed_at = NOW - timedelta(minutes=1)
    _report_store(sessions, nodes[0], held, received=first_scan)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=provider,
        build_archive_available=lambda _digest, _size: True,
        memory_floor_bytes=50,
    )
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    op_id = operation.operation_id

    def copies() -> list[str]:
        with sessions() as session:
            row = session.get(Job, op_id)
            assert row is not None
            stored = _stored_job_plan(row)
        assert stored is not None
        return [p.subphase for p in stored.phases if p.subphase == "target-copy"]

    def fail_attempt() -> None:
        with sessions.begin() as session:
            row = session.get(Job, op_id)
            assert row is not None and row.result is not None
            progress = _read_progress(row.result)
            progress.force_replan = True
            progress.subphase = "target-copy"
            row.result = _persisted_result(progress)
            row.updated_at = failed_at

    assert not copies()
    fail_attempt()
    assert service._refresh_blocked_plan(op_id, NOW)
    assert copies()
    _report_store(sessions, nodes[0], held, received=NOW, observed=timedelta(seconds=2))
    fail_attempt()
    assert service._refresh_blocked_plan(op_id, NOW)
    assert not copies()


def test_new_plan_after_a_definite_target_copy_failure_waits_for_a_newer_scan(
    tmp_path,
) -> None:
    """A copy that ended failed (no re-plan of its own) still makes the next
    plan for that Spark and model ignore the scan that pre-dates it."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    request = _request(sessions, nodes[0])
    manifest = provider.resolve_artifact_set(
        model_content_sha256=request.model_content_sha256
    )
    held = [
        {"sha256": spec.sha256, "bytes": spec.expected_bytes}
        for spec in manifest.artifacts
    ]
    _report_store(sessions, nodes[0], held, received=NOW - timedelta(minutes=2))
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=provider,
        build_archive_available=lambda _digest, _size: True,
        memory_floor_bytes=50,
    )

    def copies() -> list[str]:
        plan = service.preview(request, actor="admin")
        return [p.subphase for p in plan.phases if p.subphase == "target-copy"]

    assert not copies()
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=service.preview(request, actor="admin").plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.result is not None
        progress = _read_progress(row.result)
        progress.subphase = "target-copy"
        row.result = _persisted_result(progress)
        row.state = LifecycleState.FAILED.value
        row.updated_at = NOW - timedelta(minutes=1)

    assert copies()
    _report_store(sessions, nodes[0], held, received=NOW, observed=timedelta(seconds=2))
    assert not copies()


def test_scan_floor_reads_jobs_once_however_many_nodes(tmp_path) -> None:
    sessions, _lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=3
    )
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    manifest = provider.resolve_artifact_set(
        model_content_sha256=_request(sessions, nodes[0]).model_content_sha256
    )
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    def job_selects(node_ids) -> int:
        statements.clear()
        with sessions() as session:
            event.listen(session.get_bind(), "before_cursor_execute", record)
            try:
                missing_spark_bytes_by_node(session, manifest, node_ids, now=NOW)
            finally:
                event.remove(session.get_bind(), "before_cursor_execute", record)
        return sum("from jobs" in statement for statement in statements)

    assert job_selects(nodes[:1]) == job_selects(nodes) == 1

    # A pass over many candidates (the library) reads the jobs once in total.
    with failed_copies_read_once():
        statements.clear()
        with sessions() as session:
            event.listen(session.get_bind(), "before_cursor_execute", record)
            try:
                for group in (nodes[:1], nodes[1:], nodes):
                    missing_spark_bytes_by_node(session, manifest, group, now=NOW)
            finally:
                event.remove(session.get_bind(), "before_cursor_execute", record)
        assert sum("from jobs" in statement for statement in statements) == 1


def test_covered_claims_read_each_newest_report_in_one_statement(tmp_path) -> None:
    """Catches one newest-report query per reservation."""
    from vonk_agent_protocol import (
        InstallationNodeState,
        InstallationState,
        ReservationState,
    )
    from vonk_control.covered_installation_claims import (
        release_covered_installation_claims,
    )
    from vonk_control.models.execution import (
        InstallationNode,
        RecipeInstallation,
        ResourceReservation,
    )

    sessions, _lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=3
    )
    with sessions.begin() as session:
        for index, node_id in enumerate(nodes):
            installation = RecipeInstallation(
                id=f"installation-{index}",
                recipe_revision_id="revision",
                mapping_id="mapping",
                mapping_generation=1,
                image_digest="sha256:" + "a" * 64,
                plan_digest="b" * 64,
                plan={},
                state=InstallationState.INSTALLED,
                actor="test",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(installation)
            session.add(
                InstallationNode(
                    installation_id=installation.id,
                    node_id=node_id,
                    rank=0,
                    role="worker",
                    state=InstallationNodeState.INSTALLED,
                    required_bytes=1,
                    updated_at=NOW - timedelta(hours=1),
                )
            )
            session.add(
                ResourceReservation(
                    node_id=node_id,
                    kind="disk",
                    resource_key="disk",
                    amount_bytes=1,
                    owner_kind="installation",
                    owner_id=installation.id,
                    state=ReservationState.ACTIVE,
                    plan_digest="b" * 64,
                    created_at=NOW,
                )
            )
    for node_id in nodes:
        _report_store(sessions, node_id, [])
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    with sessions() as session:
        event.listen(session.get_bind(), "before_cursor_execute", record)
        try:
            released = release_covered_installation_claims(session, NOW)
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record)
    assert len(released) == len(nodes)
    assert (
        sum(
            "node_inventory_snapshots" in s or "node_snapshots" in s for s in statements
        )
        == 1
    )


class _FullNas(ModelCacheManifestProvider):
    """A NAS that lacks the set and has no room to download it again."""

    def download_preview(self, **kwargs):
        return super().download_preview(**kwargs) | {
            "blockers": [ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE.value]
        }


@pytest.mark.parametrize(
    ("scan", "image"),
    [
        ("held", "installed"),
        ("held", "other-recipe"),
        ("held", "missing"),
        ("empty", "other-recipe"),
    ],
)
def test_a_model_the_nas_evicted_is_downloaded_only_for_a_spark_that_lacks_it(
    tmp_path, scan: str, image: str
) -> None:
    """The NAS copy feeds only the copy of model files to a Spark. A load whose
    Sparks all hold the model's files plans no Hugging Face download after the
    NAS evicted it, whether the Spark holds the image or still needs it copied,
    and a NAS too full to take it back does not block it. A Spark lacking the
    files still needs the download, so the full NAS blocks that load."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    if image == "installed":
        installed_recipe(
            lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
        )
    elif image == "other-recipe":
        hold_image_through_another_recipe(lifecycle, sessions, now=NOW)
    request = _request(sessions, nodes[0])
    manifest = ModelCacheManifestProvider().resolve_artifact_set(
        model_content_sha256=request.model_content_sha256
    )
    held = scan == "held"
    _report_store(
        sessions,
        nodes[0],
        [
            {"sha256": spec.sha256, "bytes": spec.expected_bytes}
            for spec in manifest.artifacts
        ]
        if held
        else [],
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=_FullNas(missing_nas_bytes=manifest.expected_bytes),
        build_archive_available=lambda _digest, _size: True,
        memory_floor_bytes=50,
    )

    plan = service.preview(request, actor="admin")

    assert plan.storage.missing_nas_bytes == manifest.expected_bytes
    downloads = [phase for phase in plan.phases if phase.subphase == "model-download"]
    assert bool(downloads) is not held
    nas_blocked = [
        reason.code
        for reason in plan.blockers
        if reason.code == RunSwitchCode.NAS_DOWNLOAD_BLOCKED
    ]
    assert bool(nas_blocked) is not held
    assert plan.allowed is held, [reason.code for reason in plan.blockers]
    if image == "missing":
        # The image still travels to the Spark that holds only the files.
        assert any(phase.subphase == "target-copy" for phase in plan.phases)
    # Library readiness follows the same rule: a NAS lacking the model does
    # not make a load whose Sparks hold it read "needs download".
    nas_lacks = RecipeReadinessCheck(
        state="blocked",
        reasons=[
            RunSwitchReason(
                code=LibraryAssessmentCode.CACHE_MISSING,
                detail="The NAS lacks the model.",
                severity="blocker",
                scope="operation",
            )
        ],
    )
    readiness = LibraryAssessment._ready(
        plan, LibraryAssessment._fit(plan), nas_lacks, None
    )
    assert (readiness.state == "ready") is held, readiness


def test_each_asked_spark_holds_only_the_images_its_installed_ranks_name(
    tmp_path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        named = runtime_images_named_on_nodes(session, [*nodes, "spark-without-ranks"])

    assert named == {
        **{node_id: frozenset({build.image_digest}) for node_id in nodes},
        "spark-without-ranks": frozenset(),
    }
