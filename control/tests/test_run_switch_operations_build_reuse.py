"""Run/Switch operations: build reuse."""

from __future__ import annotations

import copy
import io
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, update
from vonk_forge_contracts import RecipeDefinition, document_sha256

from vonk_agent_protocol.state_machines import InstallationState
from vonk_control.catalog.revision_contract import (
    read_recipe_projection,
    write_catalog_projection,
)
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distribution_executor.receipts import _ChildView
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision, RecipeSourceBundle
from vonk_control.models.execution import (
    RecipeInstallation,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_builds.cancellation import (
    lock_build_dependency,
)
from vonk_control.recipe_builds.common import RecipeBuildPlan
from vonk_control.recipe_builds.service import RecipeBuildService
from vonk_control.recipe_operations.interfaces import RecipeOperationView
from vonk_control.recipe_operations.runtime_specs import (
    RUNTIME_INTERFACE,
)
from vonk_control.run_switch_contract.requests import (
    RunSwitchApplyRequest,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations.executor import RecipeLifecyclePhaseExecutor
from vonk_control.run_switch_operations.image_receipts import (
    _build_receipt_in_session,
    effective_build_receipt,
)
from vonk_control.run_switch_operations.interfaces import (
    PhaseExecution,
)
from vonk_control.run_switch_operations.service import RunSwitchOperationService
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

from .display_fixtures import reword
from .recipe_operations_support import NOW, installed_recipe, setup_services
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    ModelCacheManifestProvider,
    PendingBuilds,
    RecordingArtifactExecutor,
    _fixture_execution,
    _replace_child,
    _request,
    _runtime_receipt,
    _service,
    _target_copy_evidence,
)
from .runtime_identity_support import (
    set_inventory_identity,
)


class BuildThenCopyExecutor:
    """Drive the real build phase and retain a durable child for the test."""

    def __init__(self, lifecycle, sessions) -> None:
        self.children: dict[str, _ChildView] = {}
        self.build_preview_calls = 0
        self.build_start_calls = 0
        self.receipts: list[object] = []
        self._lifecycle = lifecycle
        self._sessions = sessions
        self._delegate = RecipeLifecyclePhaseExecutor(
            lifecycle,
            sessions,
            ClusterMappingService(sessions),
            lambda: NOW,
        )

    def execute(
        self,
        plan,
        phase,
        *,
        item_index,
        actor,
        request_key,
        progress,
    ) -> PhaseExecution:
        if phase.subphase == "container-build":
            self.build_preview_calls += 1
            execution = self._delegate.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
            if execution.operation_id is not None:
                assert execution.result is not None
                self.children[execution.operation_id] = _ChildView(
                    state="running", result=execution.result
                )
            return execution
        if phase.subphase == "target-copy":
            self.receipts.append(effective_build_receipt(plan, progress))
            return _fixture_execution(
                plan, phase, result=_target_copy_evidence(plan, phase, progress)
            )
        if phase.subphase == "runtime-image":
            with self._sessions() as session:
                build = session.get(RecipeBuild, plan.recipe_build_id)
                assert build is not None
                assert build.image_digest is not None
                assert build.oci_layout_sha256 is not None
                assert build.image_bytes is not None
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "runtime_image": _runtime_receipt(
                            plan,
                            image=build.image_digest,
                            layout=build.oci_layout_sha256,
                            size=build.image_bytes,
                            build_id=build.id,
                        ),
                        "image_digest": build.image_digest,
                        "oci_layout_sha256": build.oci_layout_sha256,
                        "image_bytes": build.image_bytes,
                    },
                )
        if phase.subphase in {"runtime-plan", "runtime-install"}:
            if phase.subphase == "runtime-plan":
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "installation_id": str(uuid.uuid4()),
                        "mapping_id": str(uuid.uuid4()),
                        "install_plan_digest": plan.plan_digest,
                        "compiled_plan_persisted": True,
                    },
                )
            return _fixture_execution(
                plan, phase, result={"installation_id": str(uuid.uuid4())}
            )
        return _fixture_execution(plan, phase, result={"phase": phase.kind})

    def get(self, operation_id: str) -> _ChildView:
        return self.children[operation_id]


@pytest.mark.usefixtures("damaged_json_rows")
def test_uncached_build_receipt_reaches_copy_after_restart_without_replay(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        build.state = "planned"
        build.image_digest = None
        build.oci_layout_sha256 = None
        build.image_bytes = None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        # Keep the complete persisted build envelope.  A restart consumes the
        # durable executable plan; an identity-only fixture is malformed DB
        # state and must fail closed.
        session.add(
            RecipeSourceBundle(
                sha256=build.source_bundle_sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key="source-bundle-uncached-build",
                manifest={"schema_version": 1},
                verified_at=NOW,
            )
        )
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
        build_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=nodes[0],
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload={"platform": "linux/arm64"},
        )

    child_id = str(uuid.uuid4())
    build_preview_calls: list[str] = []
    build_start_calls: list[str] = []
    build_start_plans: list[RecipeBuildPlan] = []

    def preview_build(recipe_revision_id, builder_node_id):
        del recipe_revision_id
        build_preview_calls.append(builder_node_id)
        return replace(
            build_plan,
            build_id=str(uuid.uuid4()),
            build_input_sha256="a" * 64,
        )

    def start_build(plan, **_kwargs) -> RecipeOperationView:
        build_start_calls.append("start")
        build_start_plans.append(plan)
        return RecipeOperationView(
            id=child_id,
            kind="recipe.build.v1",
            owner_id=build_id,
            state="building",
            plan_digest=plan.build_input_sha256,
            nodes=(),
            lifecycle_result=None,
        )

    lifecycle.preview_build = preview_build
    lifecycle.build = start_build
    executor = BuildThenCopyExecutor(lifecycle, sessions)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.build.state == "planned"
    assert plan.build.image_digest is None
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:4]] == [
        ("prepare", "container-build"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
    ]
    second_plan = service.preview(request, actor="admin")
    assert second_plan.allowed, [reason.code for reason in second_plan.blockers]
    assert second_plan.plan_digest == plan.plan_digest

    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.current_phase == "prepare"
    assert waiting.progress.subphase == "container-build"
    assert waiting.result is not None
    assert waiting.result.child_operation_id == child_id
    # Apply consumes the plan persisted during preview.  A fresh planner call
    # would admit mutable builder evidence and can derive a new identity.
    assert build_preview_calls == []
    assert build_start_calls == ["start"]
    assert build_start_plans[0].build_id == build_id
    assert build_start_plans[0].build_input_sha256 == build_plan.build_input_sha256
    assert (
        build_start_plans[0].agent_payload["recipe_content_sha256"]
        == revision.content_digest
    )

    restarted = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    # A restart records the immutable observation deadline once.
    assert restarted.tick() is True
    assert restarted.tick() is False
    assert build_preview_calls == []
    assert build_start_calls == ["start"]

    _replace_child(executor, child_id, state="succeeded")
    with sessions.begin() as session:
        completed = session.get(RecipeBuild, build_id)
        assert completed is not None
        completed.state = "succeeded"
        completed.image_digest = "sha256:" + "1" * 64
        completed.oci_layout_sha256 = "3" * 64
        completed.image_bytes = 30
    assert restarted.tick() is True
    resumed = restarted.get(operation.operation_id)
    assert resumed.current_phase == "prepare"
    assert resumed.progress.subphase == "runtime-image"
    assert resumed.result is not None
    receipt = effective_build_receipt(plan, resumed.result)
    assert receipt is not None
    assert receipt.model_dump(mode="json") == {
        "build_id": build_id,
        "build_input_sha256": build_plan.build_input_sha256,
        "image_digest": "sha256:" + "1" * 64,
        "oci_layout_sha256": "3" * 64,
        "image_bytes": 30,
    }
    assert restarted.tick() is True
    assert restarted.get(operation.operation_id).progress.subphase == "runtime-plan"
    assert restarted.tick() is True
    assert restarted.get(operation.operation_id).current_phase == "transfer"
    assert restarted.get(operation.operation_id).progress.subphase == "target-copy"
    assert restarted.tick() is True
    assert executor.receipts == [receipt]


@pytest.mark.usefixtures("damaged_json_rows")
def test_first_profile_preparation_preview_replans_a_missing_build_archive(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
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
        if session.get(RecipeSourceBundle, build.source_bundle_sha256) is None:
            session.add(
                RecipeSourceBundle(
                    sha256=build.source_bundle_sha256,
                    media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                    archive_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    storage_key="restored-source-bundle",
                    manifest={"schema_version": 1},
                    verified_at=NOW,
                )
            )
        build_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )

    def preview_build(recipe_revision_id: str, builder_node_id: str) -> RecipeBuildPlan:
        # RecipeBuildService persists through its own short transaction while
        # Run/Switch still holds the outer profile-preparation Session.
        with sessions.begin() as session:
            build = session.get(RecipeBuild, build_id)
            assert build is not None
            build.state = "planned"
            build.image_digest = None
            build.oci_layout_sha256 = None
            build.image_bytes = None
            build.updated_at = NOW + timedelta(seconds=1)
        return build_plan

    lifecycle.preview_build = preview_build
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        build_archive_available=lambda _digest, _size: False,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    with sessions() as session:
        before = [(row.id, row.state) for row in session.scalars(select(RecipeBuild))]
        jobs = tuple(session.scalars(select(Job.id)))
    inspected = service.inspect_candidate(
        request.recipe_revision_id, (nodes[0],), actor="viewer"
    )
    assert not inspected.allowed
    with sessions() as session:
        assert [
            (row.id, row.state) for row in session.scalars(select(RecipeBuild))
        ] == before
        assert tuple(session.scalars(select(Job.id))) == jobs
    # The identical normal planning path below really invokes the writer.
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.installation_id is None
    assert plan.build.build_id == build_id
    assert plan.build.state == "planned"
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:3]] == [
        ("prepare", "container-build"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
    ]


@pytest.mark.usefixtures("damaged_json_rows")
def test_editorial_successor_reuses_an_identity_matched_build(tmp_path: Path) -> None:
    """A notes-only successor reuses the identical prepared build.

    ``persist_plan_in_session`` deliberately reuses a succeeded receipt across a
    revision boundary when the executable input identity matches, keeping the
    older row and its revision id.  The build selection used to reject that row
    because its recorded revision id no longer matched the head, so an editorial
    rename planned a full rebuild of identical bytes.
    """

    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
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
        if session.get(RecipeSourceBundle, build.source_bundle_sha256) is None:
            session.add(
                RecipeSourceBundle(
                    sha256=build.source_bundle_sha256,
                    media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                    archive_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    storage_key="editorial-source-bundle",
                    manifest={"schema_version": 1},
                    verified_at=NOW,
                )
            )
        document = copy.deepcopy(revision.document)
        reword(document, "Editorially renamed recipe")
        canonical = RecipeDefinition.model_validate(document)
        successor = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=revision.document_id,
            kind=revision.kind,
            publisher=revision.publisher,
            slug=revision.slug,
            revision_number=revision.revision_number + 1,
            schema_version=2,
            state="active",
            document=canonical.model_dump(mode="json"),
            content_digest=document_sha256(canonical.model_dump(mode="json")),
            artifact_key="b" * 64,
            execution_key="c" * 64,
            source_bundle_sha256=revision.source_bundle_sha256,
            projected=copy.deepcopy(revision.projected),
            created_by="test",
            created_at=NOW,
        )
        session.add(successor)
        session.flush()
        successor_id = successor.id
        model_digest = canonical.models[0].model.content_sha256
        reused_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=successor_id,
            recipe_content_sha256=document_sha256(canonical.model_dump(mode="json")),
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )

    def preview_build(recipe_revision_id: str, builder_node_id: str) -> RecipeBuildPlan:
        del builder_node_id
        assert recipe_revision_id == successor_id
        return reused_plan

    lifecycle.preview_build = preview_build
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    plan = service.preview(
        RunSwitchPreviewRequest(
            model_content_sha256=model_digest,
            recipe_revision_id=successor_id,
            spark_group=SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=nodes[0],
                        rank=0,
                        role="entrypoint",
                        endpoint_owner=True,
                    )
                ]
            ),
            alias="qwen",
            action="run",
        ),
        actor="admin",
    )

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.build is not None
    assert plan.build.build_id == build_id
    assert plan.build.state == "available"
    assert not any(
        phase.kind == "prepare" and phase.subphase == "container-build"
        for phase in plan.phases
    )

    # The successor's own mapping is admitted against the reused build: the
    # build keeps its original owner and the successor uses it by content.
    mappings = ClusterMappingService(sessions)
    successor_mapping = mappings.materialize(
        mappings.preview(successor_id, nodes, {}, "admin"), actor="admin", now=NOW
    )
    admitted = lifecycle._install_admission.plan_install(
        successor_mapping, build_id, now=NOW
    )
    assert admitted.allowed

    # The container-build receipt path and the build's consumer lock take the
    # reused build for the successor plan: an image is its content, not the
    # revision that first built it.
    with sessions.begin() as session:
        receipt = _build_receipt_in_session(session, plan)
        reused = session.get(RecipeBuild, build_id)
        assert reused is not None
        assert reused.recipe_revision_id != successor_id
        assert (
            lock_build_dependency(
                session,
                recipe_revision_id=successor_id,
                builder_node_id=reused.builder_node_id,
                build_input_sha256=reused.build_input_sha256,
                build_id=reused.id,
            )
            is reused
        )
    assert receipt.build_id == build_id
    assert receipt.state == "succeeded"


def test_present_rebuilt_image_replaces_installation_bound_to_missing_build(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, old_build_id, nodes = setup_services(
        tmp_path
    )
    installed_recipe(
        lifecycle,
        mapping_id,
        old_build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    new_build_id = str(uuid.uuid4())
    new_layout = "5" * 64
    with sessions.begin() as session:
        old_build = session.get(RecipeBuild, old_build_id)
        assert old_build is not None
        installation = session.scalar(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_build_id == old_build_id
            )
        )
        assert installation is not None
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
                state="succeeded",
                policy_report=dict(old_build.policy_report),
                plan=new_plan,
                image_digest="sha256:" + "2" * 64,
                oci_layout_sha256=new_layout,
                image_bytes=30,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
            )
        )

    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        build_archive_available=lambda digest, _size: digest == new_layout,
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.build.build_id == new_build_id
    assert plan.installation_id is None
    assert ("prepare", "runtime-plan") in {
        (phase.kind, phase.subphase) for phase in plan.phases
    }


def _installed_service(sessions, lifecycle, *, archive_present: bool):
    return RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        build_archive_available=lambda _digest, _size: archive_present,
        memory_floor_bytes=50,
    )


def _phase_kinds(plan) -> list[str]:
    return [phase.kind for phase in plan.phases]


def test_installed_recipe_starts_after_its_nas_image_expired(tmp_path: Path) -> None:
    """The installed image decides; the NAS archive and build row are provenance."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )

    plan = _installed_service(sessions, lifecycle, archive_present=False).preview(
        _request(sessions, nodes[0]), actor="admin"
    )

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.installation_id is not None
    assert plan.installation_state == InstallationState.INSTALLED
    assert _phase_kinds(plan) == ["start", "final_verify"]
    assert plan.build.state != "planned"
    with sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(RecipeBuild)) == 1


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
        assert build.image_digest
        assert build.oci_layout_sha256
        assert build.image_bytes
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
        clock=lambda: lifecycle._clock(),  # noqa: PLW0108 -- reads the replaceable clock or attribute late
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
        clock=lambda: lifecycle._clock(),  # noqa: PLW0108 -- reads the replaceable clock or attribute late
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
