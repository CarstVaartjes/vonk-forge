from __future__ import annotations

import hashlib
import io
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    ReservationState,
    canonical_message,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.catalog.revision_contract import (
    read_recipe_projection,
    write_catalog_projection,
)
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    ResourceReservation,
    RunNode,
)
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.recipe_operations import (
    RecipeBuildService,
    RecipeOperationService,
    RecipeOperationView,
)
from vonk_control.recipe_runtime_specs import (
    RUNTIME_INTERFACE,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchContainerBuildResult,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationService,
    _phase_result,
)
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

from .recipe_operations_support import (
    NOW,
    installed_recipe,
    setup_services,
)
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    ModelCacheManifestProvider,
    RecordingArtifactExecutor,
    SynchronousPhaseExecutor,
    _child_operation_id,
    _installed_service,
    _phase_kinds,
    _replace_child,
    _request,
    _service,
    _target_copy_evidence,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_identity,
)


class PendingBuilds(RecipeBuildService):
    """Small build planner double that preserves the real build contract."""

    def __init__(
        self,
        sessions,
        *,
        build_id: str,
        builder_node_id: str,
        revision_id: str,
        source_digest: str,
        template_plan: dict[str, object],
        template_policy_report: dict[str, object],
    ):
        self.sessions = sessions
        self.build_id = build_id
        self.builder_node_id = builder_node_id
        self.revision_id = revision_id
        self.source_digest = source_digest
        self.template_plan = template_plan
        self.template_policy_report = template_policy_report
        self.calls: list[str] = []

    def reusable_build_id(self, *_args, **_kwargs):
        return None

    def plan(self, recipe_revision_id: str, builder_node_id: str, *, now):
        self.calls.append(builder_node_id)
        assert recipe_revision_id == self.revision_id
        with self.sessions.begin() as session:
            row = session.get(RecipeBuild, self.build_id)
            if row is None:
                row = RecipeBuild(
                    id=self.build_id,
                    recipe_revision_id=self.revision_id,
                    builder_node_id=builder_node_id,
                    source_bundle_sha256=self.source_digest,
                    build_input_sha256="d" * 64,
                    state="planned",
                    policy_report=self.template_policy_report,
                    plan={
                        **self.template_plan,
                        "build_id": self.build_id,
                        "recipe_revision_id": self.revision_id,
                        "source_bundle_sha256": self.source_digest,
                        "build_input_sha256": "d" * 64,
                    },
                    created_at=NOW,
                    updated_at=NOW,
                )
                session.add(row)
        return RecipeBuildPlan(
            build_id=self.build_id,
            recipe_revision_id=self.revision_id,
            recipe_content_sha256="e" * 64,
            builder_node_id=builder_node_id,
            source_bundle_sha256=self.source_digest,
            build_input_sha256="d" * 64,
            agent_payload={"platform": "linux/arm64"},
        )


@pytest.mark.parametrize("newer_state", ["failed", "planned"])
def test_a_newer_unfinished_build_row_keeps_the_installation_once_its_image_expired(
    tmp_path: Path, newer_state: str
) -> None:
    sessions, lifecycle, _queue, mapping_id, old_build_id, nodes = setup_services(
        tmp_path
    )
    installed_recipe(
        lifecycle, mapping_id, old_build_id, nodes, request_id=str(uuid.uuid4())
    )
    with sessions.begin() as session:
        old_build = session.get(RecipeBuild, old_build_id)
        assert old_build is not None
        new_build_id = str(uuid.uuid4())
        new_plan = dict(old_build.plan)
        new_plan["build_id"] = new_build_id
        new_plan["build_input_sha256"] = "6" * 64
        session.add(
            RecipeBuild(
                id=new_build_id,
                recipe_revision_id=old_build.recipe_revision_id,
                builder_node_id=old_build.builder_node_id,
                source_bundle_sha256=old_build.source_bundle_sha256,
                build_input_sha256="6" * 64,
                state=newer_state,
                policy_report=dict(old_build.policy_report),
                plan=new_plan,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
            )
        )

    plan = _installed_service(sessions, lifecycle, archive_present=False).preview(
        _request(sessions, nodes[0]), actor="admin"
    )

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.installation_id is not None
    assert _phase_kinds(plan) == ["start", "final_verify"]
    with sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(RecipeBuild)) == 2


def _build_service_that_reaches_the_archive_check(
    tmp_path: Path, sessions, build_id: str, archive_present
) -> RecipeBuildService:
    """A real build service whose resolve() gets as far as the archive check.

    The fixture's source bundle is stored and its build row carries the input
    identity the current recipe reproduces, so resolve() only stops at the
    archive callable.
    """

    bundles = SourceBundleStore(tmp_path / "sources")
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 10001:10001\n"})
    bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    builder_digest = "a" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(
                projected=write_catalog_projection(read_recipe_projection(revision)),
                source_bundle_sha256=bundle.sha256,
            )
        )
        build.source_bundle_sha256 = bundle.sha256
        build.policy_report = {
            **build.policy_report,
            "source_bundle_sha256": bundle.sha256,
            "builder_binary_digest": builder_digest,
        }
        revision_id = revision.id
    probe = RecipeBuildService(
        sessions, bundles=bundles, build_archive_available=lambda *_: True
    )
    resolution = probe._resolve_once(revision_id)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        build.build_input_sha256 = resolution.build_input_for_builder(builder_digest)
    assert probe.reusable_build_id(revision_id) == build_id
    return RecipeBuildService(
        sessions, bundles=bundles, build_archive_available=archive_present
    )


def test_installed_reuse_is_decided_without_asking_the_nas_archive(
    tmp_path: Path,
) -> None:
    """Which wrong implementation: reuse that still consults archive presence.

    Production wiring: the real build service and the planner share the same
    archive check, and resolve() reaches it for this recipe (proved below), so
    any reuse decision that resolves a build raises.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )

    def never_asked(_digest, _size):
        raise AssertionError("installation reuse must not read NAS archive presence")

    lifecycle._builds = _build_service_that_reaches_the_archive_check(
        tmp_path, sessions, build_id, never_asked
    )
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision_id = build.recipe_revision_id
    with pytest.raises(AssertionError, match="archive presence"):
        lifecycle.reusable_build_id(revision_id)

    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        build_archive_available=never_asked,
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert _phase_kinds(plan) == ["start", "final_verify"]


def _accepted_identity(sessions, build_id: str) -> RuntimeImageIdentity:
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        assert build.image_digest and build.oci_layout_sha256 and build.image_bytes
        return RuntimeImageIdentity(
            image_digest=build.image_digest,
            oci_layout_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
            architecture="linux-arm64",
            runtime_interface=RUNTIME_INTERFACE,
            build_id=build.id,
        )


@pytest.mark.parametrize("accepted", [True, False])
def test_installed_reuse_never_enters_build_resolution(
    tmp_path: Path, monkeypatch, accepted: bool
) -> None:
    """Which wrong implementation: resolve() (source bundle, row scan) per pass."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    resolutions: list[str] = []
    original = lifecycle.reusable_build_id
    monkeypatch.setattr(
        lifecycle,
        "reusable_build_id",
        lambda revision_id: resolutions.append(revision_id) or original(revision_id),
    )
    service = _installed_service(sessions, lifecycle, archive_present=False)
    request = _request(sessions, nodes[0])
    plan = (
        service.inspect_request(
            request,
            actor="admin",
            expected_runtime_image=_accepted_identity(sessions, build_id),
        )
        if accepted
        else service.preview(request, actor="admin")
    )

    assert _phase_kinds(plan) == ["start", "final_verify"]
    assert resolutions == []


def test_installed_reuse_cost_does_not_grow_with_build_rows(tmp_path: Path) -> None:
    """Which wrong implementation: a scan of every build row per planning pass."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _installed_service(sessions, lifecycle, archive_present=False)
    request = _request(sessions, nodes[0])
    statements: list[str] = []
    engine = sessions.kw["bind"]

    def count(_conn, _cursor, statement, *_rest):
        # Primary-key reads are one row; every other read must be bounded.
        if "recipe_builds" in statement and "recipe_builds.id = " not in statement:
            statements.append(statement)

    def measure() -> int:
        statements.clear()
        event.listen(engine, "before_cursor_execute", count)
        try:
            plan = service.preview(request, actor="admin")
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert _phase_kinds(plan) == ["start", "final_verify"]
        assert all("LIMIT" in statement for statement in statements), statements
        return len(statements)

    few = measure()
    with sessions.begin() as session:
        base = session.get(RecipeBuild, build_id)
        assert base is not None
        for index in range(80):
            row_id = str(uuid.uuid4())
            session.add(
                RecipeBuild(
                    id=row_id,
                    recipe_revision_id=base.recipe_revision_id,
                    builder_node_id=base.builder_node_id,
                    source_bundle_sha256=base.source_bundle_sha256,
                    build_input_sha256=f"{index + 16:064x}",
                    state="succeeded",
                    policy_report=dict(base.policy_report),
                    plan={**base.plan, "build_id": row_id},
                    image_digest=base.image_digest,
                    oci_layout_sha256=base.oci_layout_sha256,
                    image_bytes=base.image_bytes,
                    created_at=NOW - timedelta(seconds=index + 1),
                    updated_at=NOW - timedelta(seconds=index + 1),
                )
            )
    assert measure() == few


def test_accepted_image_reuse_reaches_start_and_final_verify(tmp_path: Path) -> None:
    """A profile load bound to the accepted image reuses the installation too."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    accepted = _accepted_identity(sessions, build_id)

    service = _installed_service(sessions, lifecycle, archive_present=False)
    plan = service.inspect_request(
        _request(sessions, nodes[0]), actor="admin", expected_runtime_image=accepted
    )
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert _phase_kinds(plan) == ["start", "final_verify"]
    assert plan.preparation is not None
    assert plan.preparation.runtime_image.image_digest == accepted.image_digest

    other = accepted.model_copy(update={"image_digest": "sha256:" + "d" * 64})
    assert _phase_kinds(
        service.inspect_request(
            _request(sessions, nodes[0]), actor="admin", expected_runtime_image=other
        )
    ) != ["start", "final_verify"]


@pytest.mark.parametrize("stale_state", ["succeeded", "planned"])
def test_republished_source_bundle_builds_a_new_image_for_the_same_revision(
    tmp_path: Path, stale_state: str
) -> None:
    """A package republished with a repaired Dockerfile keeps the recipe document.

    The sync re-imports it onto the same revision with a new source bundle
    digest. The earlier bundle's build (finished or still pending) and the
    installation of its image are another image, so the review plans a build of
    the current bundle and a fresh installation instead of reusing them.
    """

    sessions, lifecycle, _queue, mapping_id, old_build_id, nodes = setup_services(
        tmp_path
    )
    if stale_state == "succeeded":
        installed_recipe(
            lifecycle, mapping_id, old_build_id, nodes, request_id=str(uuid.uuid4())
        )
    new_source = "f" * 64
    new_build_id = str(uuid.uuid4())
    with sessions.begin() as session:
        old_build = session.get(RecipeBuild, old_build_id)
        assert old_build is not None
        if stale_state == "planned":
            old_build.state = "planned"
            old_build.image_digest = None
            old_build.oci_layout_sha256 = None
            old_build.image_bytes = None
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = [*snapshot.capabilities, "recipe.build.v1"]
        revision = session.get(CatalogDocumentRevision, old_build.recipe_revision_id)
        assert revision is not None
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(source_bundle_sha256=new_source)
        )
        lifecycle._builds = PendingBuilds(
            sessions,
            build_id=new_build_id,
            builder_node_id=nodes[0],
            revision_id=revision.id,
            source_digest=new_source,
            template_plan=dict(old_build.plan),
            template_policy_report=dict(old_build.policy_report),
        )

    plan = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor()).preview(
        _request(sessions, nodes[0]), actor="admin"
    )

    assert plan.build is not None
    assert plan.build.build_id == new_build_id
    assert plan.build.state == "planned"
    assert plan.installation_id is None
    assert ("prepare", "container-build") in {
        (phase.kind, phase.subphase) for phase in plan.phases
    }


def test_model_cache_manifest_failure_is_a_typed_blocker(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=ModelCacheManifestProvider(fail=True),
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed is False


def test_a_spark_whose_agent_cannot_pull_images_asks_for_an_upgrade(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = [
            item for item in snapshot.capabilities if item != "recipe.image.pull.v1"
        ]
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed is False
    upgrade = [
        reason
        for reason in plan.blockers
        if reason.code == "run-switch.agent-upgrade-required"
    ]
    assert [reason.node_ids for reason in upgrade] == [[nodes[0]]]


@pytest.mark.usefixtures("damaged_json_rows")
def test_uncached_run_selects_external_fresh_builder_and_plans_container_phase(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    source_digest = "c" * 64
    builder_id = "spk_" + "9" * 32
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        template_plan = dict(build.plan)
        template_policy_report = dict(build.policy_report)
        session.delete(build)
        session.add(
            RecipeSourceBundle(
                sha256=source_digest,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key="source-bundle-c",
                manifest={"schema_version": 1},
                verified_at=NOW,
            )
        )
        session.add(
            AgentNode(
                node_id=builder_id,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    builder_id,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {
                            **PACKAGED_RUNTIME_IDENTITY,
                            "architecture": "linux-arm64",
                            "binary_digest": "a" * 64,
                        }
                    ),
                ),
            )
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": builder_id,
                    "observed_at": NOW,
                    "disk_total_bytes": 10_000,
                    "disk_free_bytes": 8_000,
                    "host_memory_total_bytes": 10_000,
                    "host_memory_free_bytes": 8_000,
                    "gpu_memory_total_bytes": 10_000,
                    "gpu_memory_free_bytes": 8_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1",),
                    "memory_pool": "shared",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )
    with sessions.begin() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        fake_builds = PendingBuilds(
            sessions,
            build_id=str(uuid.uuid4()),
            builder_node_id=builder_id,
            revision_id=revision.id,
            source_digest=source_digest,
            template_plan=template_plan,
            template_policy_report=template_policy_report,
        )
    lifecycle._builds = fake_builds
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed is True
    assert plan.build.state == "planned"
    assert plan.build.builder_node_id == builder_id
    assert plan.build.build_input_sha256 == "d" * 64
    assert fake_builds.calls == [builder_id]
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:4]] == [
        ("prepare", "container-build"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
    ]


@pytest.mark.usefixtures("damaged_json_rows")
def test_container_phase_delegates_to_existing_recipe_build_child(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    source_digest = "c" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        build.state = "planned"
        build.image_digest = None
        build.oci_layout_sha256 = None
        build.image_bytes = None
        build.source_bundle_sha256 = source_digest
        session.add(
            RecipeSourceBundle(
                sha256=source_digest,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key="source-bundle-build",
                manifest={"schema_version": 1},
                verified_at=NOW,
            )
        )
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        build.plan = {**build.plan, "source_bundle_sha256": source_digest}
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = ["recipe.build.v1", "recipe.image.pull.v1"]

    class _LifecycleStub(RecipeOperationService):
        """Only ``build`` is reached by the container-build subphase."""

        def __init__(self) -> None:
            pass

    lifecycle_stub = _LifecycleStub()
    child_id = str(uuid.uuid4())
    build_plan = RecipeBuildPlan(
        build_id=build_id,
        recipe_revision_id=revision.id,
        recipe_content_sha256=revision.content_digest,
        builder_node_id=nodes[0],
        source_bundle_sha256=source_digest,
        build_input_sha256="e" * 64,
        agent_payload={"platform": "linux/arm64"},
    )
    with sessions.begin() as session:
        row = session.get(RecipeBuild, build_id)
        assert row is not None
        row.build_input_sha256 = build_plan.build_input_sha256
        row.plan = {
            **row.plan,
            "build_input_sha256": build_plan.build_input_sha256,
        }

    def start_build(*_args, **_kwargs) -> RecipeOperationView:
        with sessions.begin() as session:
            _kwargs["admission_guard"](session)
            build = session.get(RecipeBuild, build_id)
            assert build is not None
            build.state = "building"
        return RecipeOperationView(
            id=child_id,
            kind="recipe.build.v1",
            owner_id=build_id,
            state="running",
            plan_digest=build_plan.build_input_sha256,
            nodes=(),
            lifecycle_result=None,
        )

    lifecycle_stub.build = start_build
    executor = RecipeLifecyclePhaseExecutor(
        lifecycle_stub,
        sessions,
        # Mapping is not touched by the container subphase.
        ClusterMappingService(sessions),
        lambda: NOW,
    )
    request_key = str(uuid.uuid4())
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    parent = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=request_key,
            plan_digest=plan.plan_digest,
        ),
        actor="admin",
    )
    assert parent.result is not None
    progress = parent.result
    phase = next(phase for phase in plan.phases if phase.subphase == "container-build")
    execution = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress=progress,
    )
    assert execution.operation_id == child_id
    assert isinstance(execution.result, RunSwitchContainerBuildResult)
    assert execution.result.build_id == build_id
    assert execution.result.build_input_sha256 == "e" * 64
    assert execution.result.state == "building"
    assert execution.result.phase == "prepare"
    assert execution.result.subphase == "container-build"
    assert _phase_result(execution.result, phase=phase) == execution.result

    # A build preview superseded in place re-plans; it does not end the load.
    from vonk_agent_protocol import InvalidRequestReason, WaitReason
    from vonk_control.recipe_operations.errors import RecipeRequestInvalid
    from vonk_control.run_switch_operations.errors import RunSwitchRetryLater

    def superseded_build(*_args, **_kwargs) -> RecipeOperationView:
        raise RecipeRequestInvalid(
            "recipe build preview is stale", reason=InvalidRequestReason.SUPERSEDED
        )

    lifecycle_stub.build = superseded_build
    with pytest.raises(RunSwitchRetryLater) as replan:
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )
    assert replan.value.typed_reason is WaitReason.SCOPE_CHANGED

    # A durable plan mutation is rejected before dispatch; execution never
    # re-plans around the changed identity.
    with sessions.begin() as session:
        row = session.get(RecipeBuild, build_id)
        assert row is not None
        row.plan = {**row.plan, "build_input_sha256": "a" * 64}
    with pytest.raises(Exception) as _ending:
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )


def test_a_refusal_that_survives_a_replan_retries_in_place_and_keeps_counting(
    tmp_path: Path,
) -> None:
    """A fresh plan that meets the same refusal again must not loop silently.

    Re-planning forgets the attempt count, so a refusal that planning cannot
    remove used to cycle fail, re-plan, fail forever and never reached the
    retry count that surfaces a named stall on the profile and in Fleet.
    """

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    now = [NOW]
    service = _service(
        sessions, NOW, lifecycle, RecordingArtifactExecutor(), phase_executor=None
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed
    parent = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=str(uuid.uuid4()),
            plan_digest=plan.plan_digest,
        ),
        actor="admin",
    )
    reason = "RuntimeError: current recipe is not authorized for OCI build receipt"

    def refuse() -> dict[str, object]:
        service._fail(
            parent.operation_id,
            reason,
            replan=True,
            checkpoint=(0, 0, None),
        )
        with sessions() as session:
            job = session.get(Job, parent.operation_id)
            assert job is not None and isinstance(job.result, dict)
            return dict(job.result)

    first = refuse()
    assert first["force_replan"] is True
    assert first["retry_attempt"] == 2
    first_due = service.get(parent.operation_id).next_attempt_at
    assert first_due is not None
    now[0] = first_due + timedelta(seconds=1)
    assert service._refresh_blocked_plan(parent.operation_id, now[0]) is True

    # The same refusal on the fresh plan is retried in place, and counted.
    second = refuse()
    assert second["force_replan"] is False
    assert second["retry_attempt"] == 2
    third = refuse()
    assert third["force_replan"] is False
    assert third["retry_attempt"] == 3


def test_exact_stop_reservation_budget_needs_a_fresh_post_stop_check(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id,
        "old",
        now=lifecycle._clock(),
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan,
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, run_id)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        run_generation = run.run_generation
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = 7_800
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = 7_800

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    # Aggregate free already accounts for the running owner. The declared
    # peak still owns the hard budget until this exact run stops.
    total = 7_800 + required + floor - 1
    free = required + floor + 10
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = free
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = free
    plan = service.preview(request, actor="admin")

    assert plan.fit_current.allowed is False
    assert plan.fit_after_stop is None
    assert plan.post_stop_memory_check is not None
    assert plan.post_stop_memory_check.stop_run_ids == [run_id]
    assert plan.allowed, plan.blockers
    assert plan.stop_before_prepare is True
    current_node = plan.fit_current.nodes[0]
    uncertainty = current_node.memory_usage_uncertainty
    assert uncertainty is not None
    sample = next(
        item for item in plan.freshness if item.source == f"spark:{node_id}:inventory"
    )
    assert uncertainty.source == "aggregate_inventory_without_run_usage"
    assert uncertainty.inventory_observed_at == sample.observed_at
    assert uncertainty.inventory_evidence_digest == sample.evidence_digest
    assert [
        (
            item.run_id,
            item.run_generation,
            item.reservation_kind,
            item.maximum_bytes,
        )
        for item in uncertainty.residual_ranges
    ] == [(run_id, run_generation, "unified-memory", 7_800)]
    assert current_node.memory_free_after_bytes is None
    round_tripped = RunSwitchPlan.model_validate_json(plan.model_dump_json())
    assert round_tripped.fit_current.nodes[0].memory_usage_uncertainty == uncertainty
    bad_stop = plan.model_dump(mode="python")
    bad_stop["post_stop_memory_check"] = {"stop_run_ids": [str(uuid.uuid4())]}
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        RunSwitchPlan.model_validate(bad_stop)
    # No Spark build needs the old workload's memory, so it keeps serving
    # through preparation and stops only right before start.
    assert [phase.kind for phase in plan.phases] == [
        "prepare",
        "prepare",
        "stop",
        "start",
        "final_verify",
    ]
    required = plan.fit_current.nodes[0].memory_required_bytes
    floor = plan.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    impossible_total = required + floor - 1
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        saved_capacity = (
            snapshot.host_memory_total_bytes,
            snapshot.host_memory_free_bytes,
            snapshot.gpu_memory_total_bytes,
            snapshot.gpu_memory_free_bytes,
        )
        snapshot.host_memory_total_bytes = impossible_total
        snapshot.host_memory_free_bytes = impossible_total
        snapshot.gpu_memory_total_bytes = impossible_total
        snapshot.gpu_memory_free_bytes = impossible_total
    physically_impossible = service.preview(request, actor="admin")
    # An estimate above the Spark is tried after the stop, with the claim cut
    # to the Spark less its floor; the stop is covered by the check.
    assert physically_impossible.post_stop_memory_check is not None
    assert physically_impossible.allowed
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        (
            snapshot.host_memory_total_bytes,
            snapshot.host_memory_free_bytes,
            snapshot.gpu_memory_total_bytes,
            snapshot.gpu_memory_free_bytes,
        ) = saved_capacity
    with sessions.begin() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
                ResourceReservation.state == "active",
            )
        )
        assert claim is not None
        claim.plan_digest = "a" * 64
    changed = service.preview(request, actor="admin")
    assert changed.post_stop_memory_check is None
    assert not changed.allowed


def _single_run_replaced_by_a_dual(
    tmp_path: Path,
    *,
    resident_on_stopped_spark: bool = False,
) -> tuple[
    sessionmaker[Session],
    RunSwitchOperationService,
    RunSwitchPreviewRequest,
    str,
    tuple[str, ...],
]:
    """One run serving on the first of two Sparks, and a switch to a dual.

    Each Spark's pool fits the dual's demand, but not that demand and the run's
    peak: on the first Spark only the run's planned stop makes room. With
    ``resident_on_stopped_spark`` the first Spark's observed free memory still
    holds the running workload, as a live Spark reports it before the stop.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation_id = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    ).owner_id
    run_id = lifecycle._run_admission.accept_run(
        lifecycle._run_admission.plan_run(
            installation_id, "old", now=lifecycle._clock()
        ),
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, run_id)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        group = SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=item.node_id,
                    rank=item.rank,
                    role=item.role,
                    endpoint_owner=item.rank == 0,
                )
                for item in session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            ]
        )
        assert [item.node_id for item in group.nodes] == list(nodes)
        # The outgoing run serves on the first Spark only.
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            if item.node_id == nodes[1]:
                session.delete(item)
            else:
                item.state = "running"
                item.reserved_memory_bytes = 7_800
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
            )
        ):
            if reservation.node_id == nodes[1]:
                session.delete(reservation)
            elif reservation.kind == "unified-memory":
                reservation.amount_bytes = 7_800

    request = _request(sessions, nodes[0], action="switch").model_copy(
        update={"spark_group": group}
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.host_memory_total_bytes = 7_800 + required + floor - 1
            snapshot.gpu_memory_total_bytes = 7_800 + required + floor - 1
            free = (
                floor
                if resident_on_stopped_spark and snapshot.node_id == nodes[0]
                else required + floor + 10
            )
            snapshot.host_memory_free_bytes = free
            snapshot.gpu_memory_free_bytes = free
    return sessions, service, request, run_id, nodes


def _independent_build(sessions: sessionmaker[Session], node_id: str) -> str:
    """A build no stop releases, holding its claims as the builder records them."""
    build_id = str(uuid.uuid4())
    build_input = hashlib.sha256(build_id.encode()).hexdigest()
    with sessions.begin() as session:
        session.add_all(
            ResourceReservation(
                node_id=node_id,
                kind=kind,
                resource_key=build_input,
                amount_bytes=amount,
                owner_kind="recipe-build",
                owner_id=build_id,
                state=ReservationState.ACTIVE,
                plan_digest=build_input,
                created_at=NOW,
            )
            for kind, amount in (("disk", 1), ("host-memory", 7_800))
        )
    return build_id


@pytest.mark.parametrize("resident_on_stopped_spark", [False, True])
def test_a_switch_waiting_on_another_claim_names_it_not_the_planned_stop(
    tmp_path: Path, resident_on_stopped_spark: bool
) -> None:
    """The stop-bound shortfall on the stopped run's Spark is cleared by its stop.

    The switch still waits, but on the claim no stop releases, and that is what
    its blockers (and so the wait's headline) name.
    """
    sessions, service, request, run_id, nodes = _single_run_replaced_by_a_dual(
        tmp_path, resident_on_stopped_spark=resident_on_stopped_spark
    )
    # Only the stop stands between the dual and its start: it is admitted
    # behind a fresh post-stop check.
    clear = service.preview(request, actor="admin")
    assert clear.post_stop_memory_check is not None
    assert clear.post_stop_memory_check.stop_run_ids == [run_id]
    assert clear.allowed, clear.blockers

    holder = _independent_build(sessions, nodes[1])
    waiting = service.preview(request, actor="admin")

    assert not waiting.allowed
    assert waiting.post_stop_memory_check is None
    # The wait's headline is the claim no stop releases, on its Spark.
    assert waiting.blockers
    assert waiting.blockers[0].node_ids == [nodes[1]]
    assert holder in waiting.blockers[0].detail, [
        (reason.code, reason.node_ids, reason.detail) for reason in waiting.blockers
    ]


def test_a_claim_beside_the_stopped_run_stays_part_of_the_wait(
    tmp_path: Path,
) -> None:
    """A claim on the stopped run's Spark that the stop does not release is named.

    Releasing the other Spark's claim alone would still leave the switch waiting
    on it, so the wait names both claims, the other Spark's first.
    """
    sessions, service, request, _run_id, nodes = _single_run_replaced_by_a_dual(
        tmp_path
    )
    beside_stop = _independent_build(sessions, nodes[0])
    other_spark = _independent_build(sessions, nodes[1])
    waiting = service.preview(request, actor="admin")

    assert not waiting.allowed
    assert waiting.post_stop_memory_check is None
    assert other_spark in waiting.blockers[0].detail
    assert any(
        reason.node_ids == [nodes[0]] and beside_stop in reason.detail
        for reason in waiting.blockers
    ), [(reason.code, reason.node_ids, reason.detail) for reason in waiting.blockers]


def test_recheck_binds_the_inventory_sample_it_reads_not_the_reviewed_one(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id,
        "old",
        now=lifecycle._clock(),
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan,
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, run_id)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = 7_800
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = 7_800

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    # Aggregate free already accounts for the running owner. The declared
    # peak still owns the hard budget until this exact run stops.
    total = 7_800 + required + floor - 1
    free = required + floor + 10
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = free
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = free
    plan = service.preview(request, actor="admin")

    assert plan.fit_current.allowed is False
    assert plan.post_stop_memory_check is not None
    reviewed_sample = next(
        item for item in plan.freshness if item.source == f"spark:{node_id}:inventory"
    )

    # The Spark reports again after the review. A parked profile admission is
    # retried minutes later, so this is the normal case, not a race.
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = snapshot.observed_at + timedelta(seconds=1)
    with sessions() as session:
        rechecked = service.recheck_resources_in_session(session, request, plan)

    assert rechecked.allowed
    sample = next(
        item
        for item in rechecked.freshness
        if item.source == f"spark:{node_id}:inventory"
    )
    assert reviewed_sample.observed_at is not None
    assert sample.observed_at == reviewed_sample.observed_at + timedelta(seconds=1)
    uncertainty = rechecked.fit_current.nodes[0].memory_usage_uncertainty
    assert uncertainty is not None
    assert uncertainty.inventory_observed_at == sample.observed_at


def test_switch_preserves_the_memory_floor_on_an_idle_spark(
    tmp_path: Path,
) -> None:
    """An idle Spark still needs host headroom; fresh capacity admits a new request."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    for headroom in (-1, 0):
        total = required + floor + headroom
        with sessions.begin() as session:
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            snapshot.host_memory_total_bytes = total
            snapshot.host_memory_free_bytes = total
            snapshot.gpu_memory_total_bytes = total
            snapshot.gpu_memory_free_bytes = total
        plan = service.preview(request, actor="admin")
        # A declared peak is an estimate: one byte short of the floor is
        # tried, claiming the Spark less its floor, with a warning.
        assert plan.fit_current.allowed
        assert plan.allowed
        node = plan.fit_current.nodes[0]
        assert node.memory_required_bytes == min(required, total - floor)
        assert any("only claim" in warning.detail for warning in plan.warnings) == (
            headroom != 0
        )


def test_switch_tries_an_estimate_above_an_idle_spark_as_its_only_claim(
    tmp_path: Path,
) -> None:
    """A declared peak is an estimate: one byte over the idle maximum is tried."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    total = required + floor - 1
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = total
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = total
    plan = service.preview(request, actor="admin")
    assert plan.allowed
    node = plan.fit_current.nodes[0]
    # The try claims the Spark less its floor (total - floor): the estimate cut
    # to what the Spark can hold, leaving the floor.
    assert node.memory_required_bytes == total - floor
    assert node.memory_free_after_bytes == floor


def test_switch_replaces_the_run_that_holds_the_nodes_capacity(
    tmp_path: Path,
) -> None:
    """The run a plan stops must not block that same plan.

    Wrong implementation this catches: the low-level run admission summed every
    active reservation, so an installed replacement reported
    ``run.insufficient_memory`` and ``run-switch.run_admission_blocked`` for the
    memory and ports the stopped run itself held.  Because the only release
    path is a successful stop, no reviewed plan could ever stop that run; live,
    that wedged a GLM load behind a 122 GB unified-memory reservation whose run
    was no longer running.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id,
        "old",
        now=lifecycle._clock(),
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan,
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "running"
        run.route_state = "published"
        # The live run holds nearly all of the node's memory, so a replacement
        # can only be admitted once the plan's own Stop phase frees it.
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = 7_900
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = 7_900

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    plan = service.preview(request, actor="admin")

    codes = {reason.code for reason in plan.blockers}
    assert "run.port_occupied" not in codes
    assert "run.rendezvous_port_occupied" not in codes
    assert "run-switch.run_admission_blocked" not in codes
    assert plan.allowed is True
    assert [stop.run_id for stop in plan.stops] == [run_id]


def test_switch_replaces_a_running_model_with_an_estimate_above_the_idle_maximum(
    tmp_path: Path,
) -> None:
    """A switch to an over-max estimate stops the old run first, then tries.

    Catches the replaced run counting as a holder that keeps the new estimate
    waiting, while the post-stop check says no stop can ever fit it: the switch
    then waited on its own stop for ever.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id, "old", now=lifecycle._clock()
    )
    estimate = run_plan.nodes[0].required_memory_bytes
    floor = run_plan.nodes[0].memory_floor_bytes
    total = estimate + floor - 1
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "running"
        run.route_state = "published"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = total - floor
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = total - floor
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        # The old run uses all but the floor; the Spark is one byte short of
        # what the estimate and the floor ask.
        snapshot.host_memory_total_bytes = total
        snapshot.gpu_memory_total_bytes = total
        snapshot.host_memory_free_bytes = floor
        snapshot.gpu_memory_free_bytes = floor

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    plan = service.preview(request, actor="admin")

    assert plan.allowed is True, plan.blockers
    assert plan.post_stop_memory_check is not None
    assert [stop.run_id for stop in plan.stops] == [run_id]
    assert plan.fit_current.nodes[0].memory_required_bytes == total - floor


def test_artifact_child_checkpoint_advances_to_verify(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    artifact_executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    pending = service.get(operation.operation_id)
    assert pending.current_phase == "transfer"
    child_id = _child_operation_id(pending)
    _replace_child(artifact_executor, child_id, state="succeeded")
    _replace_child(
        artifact_executor, child_id, result=_target_copy_evidence(plan, plan.phases[0])
    )
    assert service.tick() is True
    assert service.get(operation.operation_id).current_phase == "verify"
