from __future__ import annotations

import errno
import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from vonk_control.auth import CursorCodec
from vonk_control.cluster_mappings import ClusterMappingError, ClusterMappingService
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.model_cache import ArtifactSetManifest, ArtifactSpec
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    NodeArtifact,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.recipe_runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
)
from vonk_control.run_switch_contract import (
    InvocationMetadata,
    RunSwitchApplyRequest,
    RunSwitchPhase,
    RunSwitchPhaseResult,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    PhaseExecution,
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationConflict,
    RunSwitchOperationProvider,
    RunSwitchOperationService,
    _phase_result,
    _transient_distribution_exception,
    effective_build_receipt,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    prepare_runtime_image,
)
from vonk_control.runtime_preflight import latest_result

from .preflight_fixtures import record_passing_preflight
from .test_lifecycle_preflight import _finish
from .test_recipe_operations import (
    NOW,
    _CanonicalModelCache,
    installed_recipe,
    setup_services,
)


@pytest.mark.parametrize(
    ("kind", "subphase", "receipt"),
    [
        ("stop", None, {"phase": "start", "run_id": "11111111-1111-4111-8111-111111111111"}),
        ("prepare", "runtime-image", {"phase": "prepare", "subphase": "runtime-plan", "prepared": True}),
    ],
)
def test_valid_receipt_for_another_phase_cannot_enter_current_progress(
    kind, subphase, receipt
) -> None:
    TypeAdapter(RunSwitchPhaseResult).validate_python(receipt, strict=True)
    expected = RunSwitchPhase(
        index=0, kind=kind, subphase=subphase, state="planned", detail="Current phase"
    )
    with pytest.raises(RunSwitchOperationConflict, match="phase receipt is invalid"):
        _phase_result(receipt, phase=expected)


def test_persisted_phase_receipts_reject_empty_and_cross_phase_shapes() -> None:
    adapter = TypeAdapter(RunSwitchPhaseResult)
    with pytest.raises(ValidationError):
        adapter.validate_python({}, strict=True)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "phase": "prepare",
                "subphase": "runtime-image",
                "runtime_image": {"image_digest": "sha256:" + "1" * 64},
                "image_digest": "sha256:" + "1" * 64,
                "oci_layout_sha256": "2" * 64,
                "image_bytes": 1,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "phase": "transfer",
                "subphase": "model-download",
                "schema_version": 2,
                "artifact_set_sha256": "3" * 64,
                "downloaded_bytes": 0,
                "total_bytes": 0,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "phase": "transfer",
                "subphase": "model-download",
                "schema_version": 2,
                "artifact_set_sha256": "3" * 64,
                "coverage": "complete",
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "progress": {
                    "phase": "model-download",
                    "completed_bytes": 0,
                    "total_bytes": 0,
                    "total_bytes_known": True,
                },
                "evidence": {
                    "schema_version": 2,
                    "artifact_set_sha256": "4" * 64,
                    "coverage": "complete",
                },
            },
            strict=True,
        )

MODEL_ARTIFACT = "c" * 64
MODEL_ARTIFACT_SET = "f" * 64


def _target_copy_evidence(plan, phase, progress=None) -> dict[str, object]:
    runtime_image = getattr(getattr(plan, "preparation", None), "runtime_image", None)
    image_digest = getattr(runtime_image, "image_digest", None) or getattr(
        plan, "image_digest", "sha256:" + "1" * 64
    )
    layout_digest = getattr(runtime_image, "oci_layout_sha256", None) or getattr(
        getattr(plan, "build", None), "oci_layout_sha256", "3" * 64
    )
    if isinstance(progress, dict):
        for candidate in reversed(progress.get("phase_results", [])):
            if isinstance(candidate, dict):
                image_digest = image_digest or candidate.get("image_digest")
                layout_digest = layout_digest or candidate.get("oci_layout_sha256")
    node_id = phase.node_ids[0] if getattr(phase, "node_ids", ()) else "spk_" + "0" * 32
    return {
        "node_id": node_id,
        "verified": True,
        "verified_digests": list(getattr(plan.storage, "artifact_digests", ())) or [MODEL_ARTIFACT],
        "verified_image_digest": image_digest,
        "imported_image_digest": image_digest,
        "verified_oci_layout_sha256": layout_digest,
        "copied_bytes": getattr(plan.storage, "missing_spark_bytes", 0),
    }


def _runtime_receipt(
    plan,
    *,
    image: str | None = None,
    layout: str | None = None,
    size: int | None = None,
    build_id: str | None = None,
) -> dict[str, object]:
    image = image or getattr(plan.build, "image_digest", None) or getattr(plan, "image_digest", None)
    layout = layout or getattr(plan.build, "oci_layout_sha256", None)
    size = size or getattr(plan.build, "image_bytes", None) or 1
    build_id = build_id or getattr(plan, "recipe_build_id", None)
    return {
        "schema_version": 2,
        "source": "controller-build",
        "distribution_publisher": "test",
        "distribution_slug": "recipe",
        "distribution_content_sha256": "a" * 64,
        "registry_manifest_digest": None,
        "platform_manifest_digest": image,
        "image_digest": image,
        "oci_archive_sha256": layout,
        "image_bytes": size,
        "local_image_config_id": None,
        "local_image_reference": "localhost/test",
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "archive_path": "/tmp/runtime-image.oci.tar",
        "recorded_at": NOW.isoformat(),
        "build_id": build_id,
        "runtime_interface_label": "v1",
    }


class CompleteArtifactInspector:
    def __init__(self, *, reclaimable_bytes: int = 0, missing_spark_bytes: int = 0) -> None:
        self.reclaimable_bytes = reclaimable_bytes
        self.missing_spark_bytes = missing_spark_bytes

    def inspect(
        self,
        _session,
        *,
        model_content_sha256,
        recipe_revision_id,
        node_ids,
        retention,
        now,
    ) -> ArtifactInspection:
        required = 1024 * len(node_ids)
        missing_spark_bytes = self.missing_spark_bytes
        return ArtifactInspection(
            required_bytes=required,
            reused_bytes=required - missing_spark_bytes,
            copied_bytes=missing_spark_bytes,
            missing_nas_bytes=0,
            missing_spark_bytes=missing_spark_bytes,
            reclaimable_bytes=self.reclaimable_bytes,
            nas_coverage="complete",
            spark_coverage="complete" if missing_spark_bytes == 0 else "partial",
            artifact_digests=(MODEL_ARTIFACT,),
            reclaimable_digests=("1" * 64,) if self.reclaimable_bytes else (),
            artifact_set_sha256=MODEL_ARTIFACT_SET,
            artifact_set_bytes=1024,
        )


class ModelCacheManifestProvider:
    def __init__(self, *, missing_nas_bytes: int = 1024, fail: bool = False) -> None:
        self.missing_nas_bytes = missing_nas_bytes
        self.fail = fail

    def resolve_artifact_set(self, **kwargs):
        if self.fail:
            raise RuntimeError("trusted catalog manifest unavailable")
        model_digest = str(kwargs["model_content_sha256"])
        return ArtifactSetManifest(
            model_content_sha256=model_digest,
            recipe_revision_sha256="b" * 64,
            model_content_digests=(model_digest,),
            artifacts=(
                ArtifactSpec(
                    key="primary:weights",
                    artifact_id="weights",
                    path="weights.safetensors",
                    kind="huggingface",
                    repository="vonk-forge/primary",
                    source="https://huggingface.co/vonk-forge/primary/resolve/main/weights.safetensors",
                    revision="0" * 40,
                    sha256=MODEL_ARTIFACT,
                    expected_bytes=1024,
                    roles=("weights",),
                    model_content_sha256=model_digest,
                ),
            ),
        )

    def download_preview(self, **kwargs):
        manifest = self.resolve_artifact_set(**kwargs)
        return {
            "schema_version": 2,
            "artifact_set_sha256": manifest.digest,
            "plan_digest": "a" * 64,
            "source_policy": "nas-first",
            "artifact_count": len(manifest.artifacts),
            "expected_bytes": manifest.expected_bytes,
            "already_cached_bytes": manifest.expected_bytes - self.missing_nas_bytes,
            "new_bytes": self.missing_nas_bytes,
            "blockers": [],
            "warnings": [],
        }


class RecordingArtifactExecutor:
    def __init__(self, *, child_transfer: bool = False, bad_verify: bool = False) -> None:
        self.child_transfer = child_transfer
        self.bad_verify = bad_verify
        self.calls: list[str] = []
        self.children: dict[str, SimpleNamespace] = {}

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
        self.calls.append(phase.kind)
        if phase.kind == "transfer" and self.child_transfer:
            child_id = str(uuid.uuid4())
            self.children[child_id] = SimpleNamespace(state="queued", result=None)
            return PhaseExecution(
                operation_id=child_id,
                result=None,
            )
        if phase.kind == "verify":
            digests = ["d" * 64] if self.bad_verify else [MODEL_ARTIFACT]
            runtime_image = getattr(getattr(plan, "preparation", None), "runtime_image", None)
            image_digest = getattr(runtime_image, "image_digest", "sha256:" + "1" * 64)
            archive_sha256 = getattr(runtime_image, "oci_layout_sha256", "3" * 64)
            return PhaseExecution(
                result={
                    "verified": True,
                    "verified_digests": digests,
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": image_digest,
                    "verified_oci_layout_sha256": archive_sha256,
                }
            )
        if phase.kind == "cleanup":
            return PhaseExecution(
                result={
                    "scope": "spark-local",
                    "reclaimed_bytes": 0,
                    "protected_referenced_bytes": 0,
                    "reclaimed_digests": [],
                    "protected_digests": [],
                }
            )
        return PhaseExecution(result=_target_copy_evidence(plan, phase))

    def get(self, operation_id: str):
        return self.children.get(operation_id)


class SynchronousPhaseExecutor:
    def execute(
        self,
        _plan,
        phase,
        *,
        item_index,
        actor,
        request_key,
        progress,
    ) -> PhaseExecution:
        return PhaseExecution(result={"phase": phase.kind})


class StopOnlyLifecycle:
    def preview_stop(self, _run_id: str):
        return SimpleNamespace(plan_digest="e" * 64)


class PendingBuilds:
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


class BuildThenCopyExecutor:
    """Drive the real build phase and retain a durable child for the test."""

    def __init__(self, lifecycle, sessions) -> None:
        self.children: dict[str, SimpleNamespace] = {}
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
                self.children[execution.operation_id] = SimpleNamespace(
                    state="running",
                    result=None,
                )
            return execution
        if phase.subphase == "target-copy":
            self.receipts.append(effective_build_receipt(plan, progress))
            return PhaseExecution(result=_target_copy_evidence(plan, phase, progress))
        if phase.subphase == "runtime-image":
            with self._sessions() as session:
                build = session.get(RecipeBuild, plan.recipe_build_id)
                assert build is not None
                assert build.image_digest is not None
                assert build.oci_layout_sha256 is not None
                assert build.image_bytes is not None
                return PhaseExecution(
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
                    }
                )
        if phase.subphase in {"runtime-plan", "runtime-install"}:
            if phase.subphase == "runtime-plan":
                return PhaseExecution(
                    result={
                        "installation_id": str(uuid.uuid4()),
                        "mapping_id": str(uuid.uuid4()),
                        "install_plan_digest": plan.plan_digest,
                        "compiled_plan_persisted": True,
                    }
                )
            return PhaseExecution(result={"installation_id": str(uuid.uuid4())})
        return PhaseExecution(result={"phase": phase.kind})

    def get(self, operation_id: str):
        return self.children.get(operation_id)


class ColdStartPhaseExecutor:
    """Value-bearing phase driver for a cold preview/apply ordering test."""

    def __init__(self) -> None:
        self.events: list[str] = []

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
        del item_index, actor, request_key, progress
        identity = phase.subphase or phase.kind
        self.events.append(identity)
        if phase.subphase == "model-download":
            total = plan.storage.missing_nas_bytes
            return PhaseExecution(
                result={
                    "schema_version": 2,
                    "artifact_set_sha256": plan.preparation.model.artifact_set_sha256,
                    "coverage": "complete",
                    "downloaded_bytes": total,
                    "total_bytes": total,
                    "progress": {
                        "phase": "model-download",
                        "completed_bytes": total,
                        "total_bytes": total,
                        "total_bytes_known": True,
                    },
                }
            )
        if phase.subphase == "runtime-image":
            return PhaseExecution(
                result={
                    "runtime_image": _runtime_receipt(plan),
                    "image_digest": plan.build.image_digest,
                    "oci_layout_sha256": plan.build.oci_layout_sha256,
                    "image_bytes": plan.build.image_bytes,
                }
            )
        if phase.kind == "transfer" and phase.subphase == "target-copy":
            return PhaseExecution(result=_target_copy_evidence(plan, phase))
        if phase.kind == "verify":
            return PhaseExecution(
                result={
                    "verified": True,
                    "verified_digests": list(plan.storage.artifact_digests),
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": plan.image_digest,
                    "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                }
            )
        if phase.subphase == "runtime-plan":
            mapping = getattr(plan, "mapping", None)
            mapping_id = getattr(mapping, "mapping_id", None) or str(uuid.uuid4())
            installation_id = getattr(plan, "installation_id", None) or str(uuid.uuid4())
            return PhaseExecution(
                result={
                    "installation_id": installation_id,
                    "mapping_id": mapping_id,
                    "install_plan_digest": plan.plan_digest,
                    "compiled_plan_persisted": True,
                }
            )
        if phase.subphase == "runtime-install":
            return PhaseExecution(
                result={
                    "installation_id": getattr(plan, "installation_id", None)
                    or str(uuid.uuid4())
                }
            )
        # ``runtime-install`` represents the real admission/compile boundary
        # in this focused driver; the asserted event ordering is the contract.
        return PhaseExecution(result={"prepared": True})

    def get(self, _operation_id: str):
        raise KeyError(_operation_id)


def _request(sessions, node_id: str, *, action: str = "run", retention: str = "retain-cached"):
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        model = revision.document["models"][0]["model"]
        model_digest = model["content_sha256"]
    return RunSwitchPreviewRequest(
        model_content_sha256=model_digest,
        recipe_revision_id=revision.id,
        spark_group=SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=node_id,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                )
            ]
        ),
        alias="qwen",
        action=action,
        retention=retention,
    )


def _service(
    sessions,
    clock,
    lifecycle,
    artifact_executor,
    *,
    artifacts=None,
    phase_executor=None,
):
    return RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: clock,
        artifacts=artifacts or CompleteArtifactInspector(),
        artifact_phase_executor=artifact_executor,
        phase_executor=phase_executor,
        memory_floor_bytes=50,
    )


def test_mapping_selection_reads_typed_parameters_from_persisted_mapping(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping.parameters = {
            "engine_extension": {"enabled": False, "nullable": None},
        }

    with sessions() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        selection = _service(
            sessions,
            lifecycle._clock(),
            lifecycle,
            RecordingArtifactExecutor(),
        )._mapping_selection(mapping, mapping_nodes)

    assert selection is not None
    assert selection.parameters == {
        "engine_extension": {"enabled": False, "nullable": None}
    }


def test_mapping_selection_rejects_malformed_persisted_parameters(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping.parameters = ["malformed"]

    with sessions() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        with pytest.raises(ClusterMappingError, match="persisted mapping parameters"):
            _service(
                sessions,
                lifecycle._clock(),
                lifecycle,
                RecordingArtifactExecutor(),
            )._mapping_selection(mapping, mapping_nodes)


def test_fresh_unmapped_group_uses_default_mapping_and_install_composite(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        for item in session.scalars(
            select(ClusterMappingNode).where(ClusterMappingNode.mapping_id == mapping_id)
        ):
            session.delete(item)
        session.delete(mapping)

    artifact_executor = RecordingArtifactExecutor()
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, node_id)
    plan = service.preview(request, actor="admin")

    assert plan.allowed is True
    assert plan.mapping is not None and plan.mapping.action == "create"
    assert "run-switch.mapping_materialization_unavailable" not in {
        reason.code for reason in plan.blockers
    }
    assert plan.build.state == "available"
    assert plan.preparation is not None
    assert [phase.kind for phase in plan.phases] == [
        "prepare",
        "transfer",
        "verify",
        "prepare",
        "start",
        "final_verify",
    ]
    assert [phase.subphase for phase in plan.phases] == [
        "runtime-plan",
        "target-copy",
        "target-copy",
        "runtime-install",
        None,
        None,
    ]

    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert operation.state == "queued"
    assert operation.operation_id
    assert service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=operation.request_key,
        ),
        actor="admin",
    ).operation_id == operation.operation_id

    assert service.tick() is True
    assert service.tick() is True
    assert service.tick() is True
    assert service.tick() is True
    progressed = service.get(operation.operation_id)
    assert progressed.current_phase == "prepare"
    assert progressed.progress.subphase == "runtime-install"
    assert progressed.state == "running"
    with sessions() as session:
        created = session.scalar(
            select(ClusterMapping).where(
                ClusterMapping.placement_digest == plan.mapping.placement_digest
            )
        )
        child = session.scalar(
            select(Job).where(Job.kind == "recipe.install").order_by(Job.created_at.desc())
        )
    assert created is not None
    assert child is not None


def test_model_cache_manifest_allows_planned_nas_download(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=ModelCacheManifestProvider(missing_nas_bytes=1024),
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.storage.nas_coverage == "partial"
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:3]] == [
        ("transfer", "model-download"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
    ]
    assert plan.storage.missing_nas_bytes == 1024
    assert plan.preparation is not None
    expected_manifest = ModelCacheManifestProvider().resolve_artifact_set(
        model_content_sha256=plan.preparation.model.model_content_sha256
    )
    assert plan.preparation.model.artifact_set_sha256 == expected_manifest.digest
    assert plan.storage.artifact_set_sha256 == expected_manifest.digest
    assert plan.storage.artifact_set_bytes == plan.preparation.model.artifact_set_bytes
    assert "run-switch.nas-coverage-unknown" not in {
        reason.code for reason in plan.blockers
    }
    assert "run-switch.nas-download-required" in {
        reason.code for reason in plan.warnings
    }


def test_cold_model_and_image_plan_defers_compile_until_both_preparations(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        session.query(NodeArtifact).delete()
    inspector = CompleteArtifactInspector(missing_spark_bytes=1024)

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            value = super().inspect(*args, **kwargs)
            return replace(
                value,
                missing_nas_bytes=1024,
                nas_coverage="partial",
            )

    executor = ColdStartPhaseExecutor()
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=ColdInspector(
            missing_spark_bytes=inspector.missing_spark_bytes,
        ),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:5]] == [
        ("transfer", "model-download"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
        ("verify", "target-copy"),
    ]
    assert plan.storage.nas_coverage == "partial"
    assert plan.runtime_storage.spark_coverage == "partial"
    assert plan.preparation is not None

    operation = service.apply(
        RunSwitchApplyRequest(
            **_request(sessions, nodes[0]).model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    for _ in range(4):
        assert service.tick() is True
    assert executor.events == [
        "model-download",
        "runtime-image",
        "runtime-plan",
        "target-copy",
    ]
    assert service.get(operation.operation_id).current_phase == "verify"


def test_cold_production_phases_prepare_receipts_before_real_install_compile(
    tmp_path: Path,
) -> None:
    """Exercise the real image prep and install compiler after cold preview."""

    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        session.query(NodeArtifact).delete()
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        image_digest = build.image_digest
        layout_digest = build.oci_layout_sha256
        image_bytes = build.image_bytes
        revision_id = build.recipe_revision_id
    assert image_digest is not None
    assert layout_digest is not None
    assert image_bytes is not None

    controller_storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    archive = b"canonical-runtime-image-archive"[:image_bytes]
    assert hashlib.sha256(archive).hexdigest() == layout_digest
    (controller_storage.root / layout_digest).write_bytes(archive)

    class ColdModelCache(_CanonicalModelCache):
        ready = False

        def verified_model_objects_for_set(self, artifact_set_sha256):
            if not self.ready:
                raise RuntimeError("model bytes are not verified yet")
            return super().verified_model_objects_for_set(artifact_set_sha256)

    model_cache = ColdModelCache()

    class _BuildArchiveTransport:
        def inspect_archive(
            self,
            archive_path: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            assert archive_path.read_bytes() == archive
            return PulledImageEvidence(
                manifest_digest=image_digest,
                requested_manifest_digest=None,
                config_id="sha256:" + "4" * 64,
                local_reference="oci-archive:" + str(archive_path),
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    transport = _BuildArchiveTransport()

    def resolve_runtime_image(document, requested_digest, runtime_spec):
        assert requested_digest == image_digest
        runtime = runtime_spec["runtime"]
        receipt = controller_storage.find_verified(
            requested_digest,
            expected_architecture=runtime["architecture"],
            expected_runtime_interface=runtime["interface"],
        )
        if receipt is None:
            raise RuntimeError("verified runtime image receipt is unavailable")
        return receipt

    execution_plans = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=resolve_runtime_image,
    )
    # Keep the final compile strict/read-only.  The worker phase below is the
    # only code that writes the cold model/image receipts first.
    lifecycle._install_admission._compiled_plan_provider = (
        execution_plans.compile_installation
    )

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            value = super().inspect(*args, **kwargs)
            return replace(
                value,
                missing_nas_bytes=1024,
                nas_coverage="partial",
            )

    class ProductionColdExecutor:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.delegate = RecipeLifecyclePhaseExecutor(
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
        ):
            identity = phase.subphase or phase.kind
            self.events.append(identity)
            if phase.subphase == "model-download":
                model_cache.ready = True
                return PhaseExecution(
                    result={
                        "schema_version": 2,
                        "artifact_set_sha256": plan.preparation.model.artifact_set_sha256,
                        "coverage": "complete",
                        "downloaded_bytes": plan.storage.missing_nas_bytes,
                        "total_bytes": plan.storage.missing_nas_bytes,
                        "progress": {
                            "phase": "model-download",
                            "completed_bytes": plan.storage.missing_nas_bytes,
                            "total_bytes": plan.storage.missing_nas_bytes,
                            "total_bytes_known": True,
                        },
                    }
                )
            if phase.subphase == "runtime-image":
                with sessions() as session:
                    revision = session.get(CatalogDocumentRevision, revision_id)
                    build = session.get(RecipeBuild, build_id)
                    assert revision is not None and build is not None
                    entities = resolve_recipe_entities(session, revision.document)
                    runtime_spec = compile_runtime_spec(
                        revision.document,
                        resolved_entities=entities,
                        parameters=(
                            dict(plan.mapping.parameters)
                            if plan.mapping is not None
                            else {}
                        ),
                        role=plan.spark_group.nodes[0].role,
                        rank=plan.spark_group.nodes[0].rank,
                        package_handle={
                            "image_digest": build.image_digest,
                            "image_reference": f"localhost/vonk/recipe-build@{build.image_digest}",
                            "build_input_sha256": build.build_input_sha256,
                            "platform": "linux/arm64",
                        },
                    )
                    receipt = prepare_runtime_image(
                        revision.document,
                        runtime=runtime_spec["runtime"],
                        storage=controller_storage,
                        transport=transport,
                        build_receipt={
                            "state": build.state,
                            "build_id": build.id,
                            "image_digest": build.image_digest,
                            "oci_layout_sha256": build.oci_layout_sha256,
                            "image_bytes": build.image_bytes,
                        },
                        now=NOW,
                    )
                return PhaseExecution(
                    result={
                        "runtime_image": receipt.to_mapping(),
                        "image_digest": receipt.image_digest,
                        # Keep the phase result's established wire key; the
                        # preparation receipt itself uses its established
                        # ``oci_archive_sha256`` field.
                        "oci_layout_sha256": receipt.oci_archive_sha256,
                        "image_bytes": receipt.image_bytes,
                    }
                )
            if phase.subphase in {"runtime-plan", "runtime-install"}:
                return self.delegate.execute(
                    plan,
                    phase,
                    item_index=item_index,
                    actor=actor,
                    request_key=request_key,
                    progress=progress,
                )
            if phase.kind == "transfer" and phase.subphase == "target-copy":
                return PhaseExecution(result=_target_copy_evidence(plan, phase))
            if phase.kind == "verify":
                return PhaseExecution(
                    result={
                        "verified": True,
                        "verified_digests": list(plan.storage.artifact_digests),
                        "verified_build_id": getattr(plan, "recipe_build_id", None),
                        "verified_image_digest": plan.image_digest,
                        "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                    }
                )
            return PhaseExecution(result={"phase": phase.kind})

        def get(self, operation_id: str):
            return self.delegate.get(operation_id)

    executor = ProductionColdExecutor()
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=ColdInspector(missing_spark_bytes=1024),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert not controller_storage.find_verified(
        image_digest,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )

    request_key = str(uuid.uuid4())
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=request_key,
        ),
        actor="admin",
    )
    replay = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=request_key,
        ),
        actor="admin",
    )
    assert replay.operation_id == operation.operation_id
    assert service.tick() is True
    assert service.tick() is True
    assert executor.events == ["model-download", "runtime-image"]
    assert controller_storage.find_verified(
        image_digest,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    ) is not None

    # Controller compilation/persistence is synchronous and must not create a
    # Spark install child before target-copy verification.
    assert service.tick() is True
    planned = service.get(operation.operation_id)
    assert planned.progress.subphase == "target-copy"
    assert planned.result.child_operation_id is None
    with sessions() as session:
        installation = session.scalar(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_build_id == build_id,
            )
        )
        assert installation is not None, list(
            session.scalars(select(RecipeInstallation))
        )
        compiled_plans = installation.plan["compiled_execution_plans"]
        assert compiled_plans
        assert all(
            payload["schema_version"] == 2
            for payload in compiled_plans.values()
        )
    assert executor.events == ["model-download", "runtime-image", "runtime-plan"]

    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase == "target-copy"
    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase == "runtime-install"
    assert service.get(operation.operation_id).result.child_operation_id is None
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.progress.subphase == "runtime-install"
    install_child_id = waiting.result.child_operation_id
    assert install_child_id
    assert executor.events == [
        "model-download",
        "runtime-image",
        "runtime-plan",
        "target-copy",
        "target-copy",
        "runtime-install",
    ]

    lifecycle.record_node_result(
        install_child_id,
        nodes[0],
        succeeded=True,
        evidence={"installed_bytes": 1024},
    )
    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase in {
        "start",
        None,
    }


class _AdvancingClock:
    """Controller clock a caller can move forward inside one phase."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _SlowColdCompiler:
    """Real compiled-plan provider whose cold compiles take minutes.

    Only the elapsed time is simulated.  The compiled launch documents are the
    canonical ones the ordinary provider produces, and ordinary agent inventory
    keeps arriving while the Controller compiles, exactly as it does on a live
    Spark node.  ``slow_compiles`` bounds how many compiles pay that cost, so a
    caller can model one cold compile or a Controller that stays slow.
    """

    def __init__(self, delegate, clock, seconds, during_compile, slow_compiles=1) -> None:
        self._delegate = delegate
        self._clock = clock
        self._seconds = seconds
        self._during_compile = during_compile
        self._slow_compiles = slow_compiles
        self.compiles = 0

    def __call__(self, **kwargs):
        compiled = self._delegate(**kwargs)
        self.compiles += 1
        if self.compiles <= self._slow_compiles:
            self._clock.advance(self._seconds)
            self._during_compile()
        return compiled


class _ColdCompileExecutor:
    """Real runtime-plan/install admission with value-bearing neighbours."""

    def __init__(self, lifecycle, sessions, clock) -> None:
        self.events: list[str] = []
        self.delegate = RecipeLifecyclePhaseExecutor(
            lifecycle,
            sessions,
            ClusterMappingService(sessions),
            clock,
        )

    def preflight(self, plan, phase, *, actor, request_key, progress):
        return self.delegate.preflight(
            plan,
            phase,
            actor=actor,
            request_key=request_key,
            progress=progress,
        )

    def execute(self, plan, phase, *, item_index, actor, request_key, progress):
        identity = phase.subphase or phase.kind
        self.events.append(identity)
        if phase.subphase in {"runtime-plan", "runtime-install"}:
            return self.delegate.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        if phase.subphase == "runtime-image":
            return PhaseExecution(
                result={
                    "runtime_image": _runtime_receipt(plan),
                    "image_digest": plan.build.image_digest,
                    "oci_layout_sha256": plan.build.oci_layout_sha256,
                    "image_bytes": plan.build.image_bytes,
                }
            )
        if phase.kind == "transfer":
            return PhaseExecution(result=_target_copy_evidence(plan, phase))
        if phase.kind == "verify":
            return PhaseExecution(
                result={
                    "verified": True,
                    "verified_digests": list(plan.storage.artifact_digests),
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": plan.image_digest,
                    "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                }
            )
        return PhaseExecution(result={"phase": phase.kind})

    def get(self, operation_id: str):
        return self.delegate.get(operation_id)


def _cold_compile_switch(tmp_path: Path, *, seconds: int = 716, slow_compiles: int = 1):
    """Start a real Run/Switch whose cold compile outlives the preflight window.

    The phase gate, ``LifecyclePreflight``, install admission and compiled launch
    documents use production code; adjacent artifact phases use test adapters.
    ``hooks`` lets a caller run its own step inside the
    expensive compile, the way an operator acts while the Controller works.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    clock = _AdvancingClock(NOW)
    lifecycle._clock = clock
    admission = lifecycle._install_admission
    inventory = InventoryRepository(sessions, clock=clock)
    hooks: list = []

    def during_compile() -> None:
        # The agent keeps reporting the same authenticated capacity; only the
        # observation time moves, so inventory freshness is never the blocker.
        for node_id in nodes:
            inventory.record(
                InventorySnapshotInput(
                    node_id,
                    clock.now,
                    10_000,
                    8_000,
                    10_000,
                    8_000,
                    10_000,
                    8_000,
                    1,
                    False,
                    ("runtime.vonk.v1", "recipe.operations.v1"),
                )
            )
        for hook in hooks:
            hook()

    compiler = _SlowColdCompiler(
        admission._compiled_plan_provider,
        clock,
        seconds,
        during_compile,
        slow_compiles,
    )
    executor = _ColdCompileExecutor(lifecycle, sessions, clock)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    admitted = admission.plan_install(mapping_id, build_id, now=clock.now)
    assert admitted.allowed
    admission._compiled_plan_provider = compiler
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )

    def drive() -> None:
        """Advance one phase, answering any ordinary preflight probe."""
        service.tick()
        view = service.get(operation.operation_id)
        checkpoint = view.result.preflight
        if checkpoint is not None and checkpoint.pending_job_id:
            _finish(sessions, checkpoint, clock.now)

    return SimpleNamespace(
        sessions=sessions,
        lifecycle=lifecycle,
        admission=admission,
        mapping_id=mapping_id,
        build_id=build_id,
        nodes=nodes,
        clock=clock,
        compiler=compiler,
        executor=executor,
        service=service,
        operation=operation,
        admitted=admitted,
        hooks=hooks,
        drive=drive,
    )


def test_slow_cold_compile_refreshes_preflight_instead_of_failing_the_switch(
    tmp_path: Path,
) -> None:
    """A cold compile longer than the preflight window must not fail the run.

    A cold ``prepare/runtime-plan`` phase measured 716s: the phase gate admits
    the node against fresh preflight receipts, ``preview_install`` reads the
    same fresh receipts, and only then does the Controller compile the exact
    launch document.  ``prepare_installation`` re-plans with a fresh clock, by
    which time the 300s runtime preflight window has closed, so admission
    refuses the identical plan with ``install.plan_stale_or_blocked``.  Nothing
    about the installation changed: inventory stays fresh, and the plan digest,
    mapping generation and recipe/build authority are identical.  The switch
    must refresh the ordinary preflight probe and still create exactly one
    installation.
    """

    switch = _cold_compile_switch(tmp_path)
    sessions, clock, admission = switch.sessions, switch.clock, switch.admission
    service, executor, compiler = switch.service, switch.executor, switch.compiler
    admitted, drive = switch.admitted, switch.drive

    for _ in range(8):
        if "runtime-plan" in executor.events:
            break
        drive()
    assert "runtime-plan" in executor.events, executor.events
    assert compiler.compiles >= 1
    assert (clock.now - NOW).total_seconds() == 716

    # Isolate the blocker: only the runtime preflight receipt expired.  The
    # compiled plan identity, mapping generation, recipe/build authority and
    # inventory freshness are all unchanged across the compile.
    replanned = admission.plan_install(
        switch.mapping_id, switch.build_id, now=clock.now
    )
    assert replanned.plan_digest == admitted.plan_digest
    assert replanned.mapping_generation == admitted.mapping_generation
    assert replanned.recipe_build_id == admitted.recipe_build_id
    assert replanned.recipe_content_sha256 == admitted.recipe_content_sha256
    assert replanned.image_digest == admitted.image_digest
    assert not replanned.allowed
    assert {reason.code for node in replanned.nodes for reason in node.blockers} == {
        "runtime_preflight.stale"
    }

    # Desired behaviour: the expired ordinary preflight is refreshed and the
    # switch still reaches exactly one installation of the identical plan.
    failure = service.get(switch.operation.operation_id)
    assert failure.state != "failed", failure.status_reason
    assert failure.result.retry_attempt == 2
    assert failure.result.retry_reason == "runtime preflight expired during install compilation"
    assert failure.progress.operation.phase == "install-preflight-refresh"
    assert failure.progress.operation.completed_items == 1
    assert failure.progress.operation.observed_at == clock.now.isoformat()
    with switch.sessions() as session:
        held = session.get(Job, switch.operation.operation_id)
        assert held.updated_at.replace(tzinfo=UTC) == switch.clock.now

    for _ in range(8):
        view = service.get(switch.operation.operation_id)
        if view.state != "running" or view.progress.subphase == "runtime-install":
            break
        drive()

    with sessions() as session:
        installations = list(session.scalars(select(RecipeInstallation)))
    assert len(installations) == 1
    assert installations[0].plan_digest == admitted.plan_digest
    assert installations[0].mapping_generation == admitted.mapping_generation
    assert installations[0].plan["compiled_execution_plans"]


def test_preflight_refresh_after_a_cold_compile_stays_bounded(tmp_path: Path) -> None:
    """A Controller that never fits the window must exhaust, not loop.

    Every compile here outlives the preflight receipt it was admitted on, so
    each refreshed probe is stale again by the time acceptance re-plans.  The
    The compilation retry budget must terminate the phase independently of
    the ordinary probe budget, with no installation or queued install child.
    """

    switch = _cold_compile_switch(tmp_path, slow_compiles=99)
    service, operation = switch.service, switch.operation

    for _ in range(40):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        switch.drive()

    view = service.get(operation.operation_id)
    assert view.state == "failed"
    assert view.status_reason == (
        "run-switch.install-preflight-refresh-exhausted: "
        "3 install compilation attempts ended with expired preflight"
    )
    assert switch.executor.events.count("runtime-plan") == 3
    assert view.result.retry_attempt == 3
    assert view.result.operation.completed_items == 3
    assert "runtime-install" not in switch.executor.events
    with switch.sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []


def test_cancellation_during_expiring_compile_prevents_a_subsequent_attempt(
    tmp_path: Path,
) -> None:
    """Cancellation on the expiry path prevents another compilation attempt.

    The cancellation lands while the Controller is compiling, so the expired
    preflight hold is the first code to see it.  It has to finish the
    cancellation under the same checkpoint guard the executing path uses
    instead of queueing another probe or accepting the identical plan later.
    This does not exercise cancellation racing a successful acceptance.
    """

    switch = _cold_compile_switch(tmp_path, slow_compiles=99)
    service, operation = switch.service, switch.operation
    switch.hooks.append(
        lambda: service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=str(uuid.uuid4()),
            reason="Operator changed their mind during the cold compile",
        )
    )

    for _ in range(20):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        switch.drive()

    view = service.get(operation.operation_id)
    assert view.state == "cancelled"
    assert switch.executor.events.count("runtime-plan") == 1
    assert "runtime-install" not in switch.executor.events
    with switch.sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []


def test_preflight_receipt_disagreement_cannot_bypass_compilation_retry_bound(
    tmp_path: Path,
) -> None:
    """Repeated DB ordering races cannot evade the gate's probe counter.

    Preview sees the fresh result cached by the gate.  During each compilation,
    another writer touches an older operation, so acceptance selects its stale
    receipt by updated_at.  Before the next tick the fresh row wins again.  A
    persistently stale DB result would simply block the next preview; this
    regression exercises the repeated interleaving that can otherwise hold
    forever without the gate ever requesting a probe.
    """
    switch = _cold_compile_switch(tmp_path, seconds=1, slow_compiles=99)
    stale_time = switch.clock.now - timedelta(seconds=301)
    record_passing_preflight(switch.sessions, stale_time)
    with switch.sessions() as session:
        stale_ids = list(session.scalars(
            select(AgentOperation.id).where(
                AgentOperation.kind == "runtime.preflight.v1",
                AgentOperation.created_at == stale_time,
            )
        ))
    assert stale_ids

    def order_stale_receipts(*, latest: bool) -> None:
        with switch.sessions.begin() as session:
            for operation_id in stale_ids:
                operation = session.get(AgentOperation, operation_id)
                operation.updated_at = (
                    switch.clock.now + timedelta(seconds=1) if latest else stale_time
                )

    switch.hooks.append(lambda: order_stale_receipts(latest=True))
    for _ in range(20):
        view = switch.service.get(switch.operation.operation_id)
        if view.state not in {"queued", "running"}:
            break
        order_stale_receipts(latest=False)
        switch.drive()
        if switch.compiler.compiles:
            view = switch.service.get(switch.operation.operation_id)
            assert view.result.preflight.pending_job_id is None
            assert not view.result.preflight.attempts
            for node_id, receipt in view.result.preflight.receipts.items():
                assert receipt.observed_at == int(NOW.timestamp())
                with switch.sessions() as session:
                    selected = latest_result(
                        session, node_id, requirements_sha256=receipt.request_sha256
                    )
                assert selected.observed_at == int(stale_time.timestamp()), (
                    view.state, view.status_reason, switch.executor.events,
                    switch.compiler.compiles, receipt.request_sha256,
                )

    view = switch.service.get(switch.operation.operation_id)
    assert view.state == "failed"
    assert view.status_reason.startswith("run-switch.install-preflight-refresh-exhausted:")
    assert switch.executor.events.count("runtime-plan") == 3
    assert view.result.retry_attempt == 3
    assert view.result.operation.completed_items == 3
    assert "runtime-install" not in switch.executor.events
    with switch.sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []


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
        node.binary_digest = "a" * 64
        node.capabilities = ["recipe.build.v1"]
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = ["recipe.build.v1"]
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

    def preview_build(_revision_id, _builder_id):
        build_preview_calls.append(_builder_id)
        return replace(
            build_plan,
            build_id=str(uuid.uuid4()),
            build_input_sha256="a" * 64,
        )

    def start_build(build_plan, **_kwargs):
        build_start_calls.append("start")
        build_start_plans.append(build_plan)
        return SimpleNamespace(id=child_id, state="building", owner_id=build_id)

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
    assert waiting.result.child_operation_id == child_id
    # Apply consumes the plan persisted during preview.  A fresh planner call
    # would admit mutable builder evidence and can derive a new identity.
    assert build_preview_calls == []
    assert build_start_calls == ["start"]
    assert build_start_plans[0].build_id == build_id
    assert build_start_plans[0].build_input_sha256 == build_plan.build_input_sha256
    assert build_start_plans[0].agent_payload["recipe_content_sha256"] == revision.content_digest

    restarted = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    assert restarted.tick() is True
    assert build_preview_calls == []
    assert build_start_calls == ["start"]

    executor.children[child_id].state = "succeeded"
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
    receipt = effective_build_receipt(plan, resumed.result)
    assert receipt == {
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


def test_model_cache_manifest_failure_is_a_typed_blocker(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
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
    assert "run-switch.artifact-inspection-unavailable" in {
        reason.code for reason in plan.blockers
    }


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
                architecture="linux-arm64",
                binary_digest="a" * 64,
                capabilities=["recipe.build.v1"],
            )
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            builder_id,
            NOW,
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("recipe.build.v1",),
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
    assert "run-switch.container-build-required" in {
        reason.code for reason in plan.warnings
    }


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
        node.binary_digest = "a" * 64
        node.capabilities = ["recipe.build.v1"]
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = ["recipe.build.v1"]
    lifecycle_stub = SimpleNamespace()
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

    def start_build(*_args, **_kwargs):
        with sessions.begin() as session:
            session.get(RecipeBuild, build_id).state = "building"
        return SimpleNamespace(id=child_id, state="running", owner_id=build_id)

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
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    phase = next(phase for phase in plan.phases if phase.subphase == "container-build")
    execution = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress={},
    )
    assert execution.operation_id == child_id
    assert execution.result == {
        "build_id": build_id,
        "build_input_sha256": "e" * 64,
        "state": "building",
        "phase": "prepare",
        "subphase": "container-build",
    }
    assert _phase_result(execution.result, phase=phase) == execution.result

    # A durable plan mutation is rejected before dispatch; execution never
    # re-plans around the changed identity.
    with sessions.begin() as session:
        row = session.get(RecipeBuild, build_id)
        assert row is not None
        row.plan = {**row.plan, "build_input_sha256": "a" * 64}
    with pytest.raises(
        RunSwitchOperationConflict,
        match="run-switch.container-build-plan-invalid",
    ):
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=str(uuid.uuid4()),
            progress={},
        )


def test_resource_constrained_switch_exposes_after_stop_fit_and_orders_stop_before_prepare(
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
        StopOnlyLifecycle(),
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    plan = service.preview(request, actor="admin")

    assert plan.fit_current.allowed is False
    assert plan.fit_after_stop is not None and plan.fit_after_stop.allowed is True
    assert plan.stop_before_prepare is True
    assert [phase.kind for phase in plan.phases] == [
        "stop",
        "prepare",
        "prepare",
        "start",
        "final_verify",
    ]


def test_artifact_child_checkpoint_and_digest_mismatch_fail_closed(tmp_path: Path) -> None:
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
    child_id = pending.result.child_operation_id
    artifact_executor.children[child_id].state = "succeeded"
    artifact_executor.children[child_id].result = _target_copy_evidence(plan, plan.phases[0])
    assert service.tick() is True
    assert service.get(operation.operation_id).current_phase == "verify"

    bad_artifacts = RecordingArtifactExecutor(bad_verify=True)
    bad_service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        bad_artifacts,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    bad_plan = bad_service.preview(request, actor="admin")
    bad_operation = bad_service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=bad_plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert bad_service._advance(bad_operation.operation_id) is True
    assert bad_service._advance(bad_operation.operation_id) is True
    failed = bad_service.get(bad_operation.operation_id)
    assert failed.state == "failed"
    assert failed.status_reason == "run-switch.artifact-digest-verification-mismatch"


def test_child_distribution_progress_is_typed_and_restart_safe(tmp_path: Path) -> None:
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
    child_id = service.get(operation.operation_id).result.child_operation_id
    artifact_executor.children[child_id].state = "running"
    artifact_executor.children[child_id].result = {
        "progress": {
            "completed_bytes": 512,
            "total_bytes": 1024,
            "members": [
                {
                    "node_id": nodes[0],
                    "phase": "transfer",
                    "state": "running",
                    "completed_bytes": 512,
                    "total_bytes": 1024,
                }
            ],
        }
    }
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.progress.completed_bytes == 512
    assert waiting.progress.total_bytes == 1024
    assert waiting.progress.total_bytes_known is True
    assert waiting.progress.members[0].node_id == nodes[0]
    assert waiting.progress.members[0].completed_bytes == 512
    assert waiting.progress.members[0].total_bytes == 1024
    assert waiting.progress.members[0].state == "running"

    # The parent only stores the child ID and JSON checkpoint.  A fresh
    # service instance can project the same durable child progress.
    restarted = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    resumed = restarted.get(operation.operation_id)
    assert resumed.progress.completed_bytes == 512
    assert resumed.progress.members[0].completed_bytes == 512

    artifact_executor.children[child_id].state = "succeeded"
    artifact_executor.children[child_id].result = {
        "copied_bytes": 1024,
        "evidence": [{
            "node_id": nodes[0],
            "verified": True,
            "verified_digests": [MODEL_ARTIFACT],
            "verified_image_digest": "sha256:" + "1" * 64,
            "imported_image_digest": "sha256:" + "1" * 64,
            "verified_oci_layout_sha256": plan.preparation.runtime_image.oci_layout_sha256,
        }],
    }
    assert restarted.tick() is True
    completed_transfer = restarted.get(operation.operation_id)
    assert completed_transfer.progress.completed_bytes == 1024
    assert completed_transfer.progress.members[0].completed_bytes == 1024
    assert completed_transfer.progress.members[0].state == "succeeded"
    assert any(
        item.node_id == nodes[0]
        and item.verified is True
        for item in completed_transfer.result.phase_results
    )
    # The next durable tick consumes the persisted transfer receipts and runs
    # the real verify phase; no caller supplied progress is reconstructed.
    assert restarted.tick() is True
    verified = restarted.get(operation.operation_id)
    assert "verify" in verified.result.completed_phases


def test_transient_distribution_failure_requeues_exact_plan_and_progress(
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
    child_id = service.get(operation.operation_id).result.child_operation_id
    artifact_executor.children[child_id].state = "failed"
    artifact_executor.children[child_id].result = {
        "error_code": "agent.copy.timeout",
        "uncertain": True,
        "progress": {
            "completed_bytes": 512,
            "total_bytes": 1024,
            "members": [{
                "node_id": nodes[0],
                "state": "unknown",
                "completed_bytes": 512,
                "total_bytes": 1024,
            }],
        },
    }
    assert service.tick() is True
    queued = service.get(operation.operation_id)
    assert queued.state == "queued"
    assert queued.plan_digest == plan.plan_digest
    assert queued.progress.completed_bytes == 512
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        assert row.current_attempt == 2
        assert row.result["child_operation_id"] is None

    assert service.tick() is True
    retried = service.get(operation.operation_id)
    assert retried.state == "running"
    assert retried.plan_digest == plan.plan_digest
    assert retried.result.child_operation_id != child_id


def test_exhausted_transient_distribution_allows_bounded_operator_retry(
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
    for attempt in range(3):
        child_id = service.get(operation.operation_id).result.child_operation_id
        artifact_executor.children[child_id].state = "failed"
        artifact_executor.children[child_id].result = {
            "error_code": "agent.copy.timeout",
            "uncertain": True,
        }
        assert service.tick() is True
        if attempt < 2:
            assert service.tick() is True
    exhausted = service.get(operation.operation_id)
    assert exhausted.state == "failed"
    retry = service.retry(
        exhausted.operation_id,
        actor="operator",
        request_key=str(uuid.uuid4()),
    )
    assert retry.state == "queued"
    assert retry.plan_digest == plan.plan_digest
    with sessions() as session:
        row = session.get(Job, retry.operation_id)
        assert row is not None
        assert row.current_attempt == 1
        assert row.payload["retry"]["operator_retries"] == 1

    assert service.tick() is True
    retried_child = service.get(retry.operation_id).result.child_operation_id
    artifact_executor.children[retried_child].state = "succeeded"
    artifact_executor.children[retried_child].result = {
        "copied_bytes": 1024,
        "evidence": [{
            "node_id": nodes[0],
            "verified": True,
            "verified_digests": [MODEL_ARTIFACT],
            "verified_image_digest": "sha256:" + "1" * 64,
            "imported_image_digest": "sha256:" + "1" * 64,
            "verified_oci_layout_sha256": plan.preparation.runtime_image.oci_layout_sha256,
        }],
    }
    assert service.tick() is True


def test_run_switch_retry_classification_rejects_terminal_http_and_storage_errors() -> None:
    request = httpx.Request("GET", "https://example.invalid/artifact")
    for status in (401, 403, 404):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError("request failed", request=request, response=response)
        assert _transient_distribution_exception(error) is False
    for status in (429, 500, 503):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError("request failed", request=request, response=response)
        assert _transient_distribution_exception(error) is True
    assert _transient_distribution_exception(OSError(errno.EPERM, "permission denied")) is False
    assert _transient_distribution_exception(OSError(errno.ENOSPC, "no space left")) is False
    assert _transient_distribution_exception(OSError(errno.ECONNRESET, "reset")) is True


def test_cleanup_adapter_cannot_evict_nas_or_return_noop(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    artifact_executor = RecordingArtifactExecutor()
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(reclaimable_bytes=30),
    )
    request = _request(sessions, nodes[0], retention="reclaim-unreferenced")
    plan = service.preview(request, actor="admin")
    assert "cleanup" in [phase.kind for phase in plan.phases]

    class NasEvictingExecutor(RecordingArtifactExecutor):
        def execute(self, plan, phase, **kwargs):
            if phase.kind == "cleanup":
                return PhaseExecution(
                    result={
                        "scope": "nas",
                        "reclaimed_bytes": 30,
                        "nas_evicted": True,
                    }
                )
            return super().execute(plan, phase, **kwargs)

    bad_service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        NasEvictingExecutor(),
        artifacts=CompleteArtifactInspector(reclaimable_bytes=30),
    )
    bad_plan = bad_service.preview(request, actor="admin")
    operation = bad_service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=bad_plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert bad_service.tick() is True
    assert bad_service.get(operation.operation_id).state == "failed"
    assert "run-switch.cleanup-scope-invalid" in (
        bad_service.get(operation.operation_id).status_reason or ""
    )


def test_invocation_metadata_does_not_change_plan_digest(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    web_request = _request(sessions, nodes[0])
    cli_request = web_request.model_copy(
        update={"invocation": InvocationMetadata(origin="cli", reason="switch")}
    )
    assert service.preview(web_request, actor="admin").plan_digest == service.preview(
        cli_request, actor="admin"
    ).plan_digest


def test_activity_provider_preserves_group_and_canonical_nested_progress(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
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
    provider = RunSwitchOperationProvider(service)
    page = provider.list_operations(
        SimpleNamespace(after=None, limit=10, state=None, node_id=None)
    )
    assert page.total == 1
    item = page.items[0]
    assert item["id"] == operation.operation_id
    assert item["job_id"] == operation.operation_id
    assert item["node_ids"] == list(nodes)
    assert item["node_id"] == nodes[0]
    assert item["attempt"] >= 1
    assert item["supported_actions"] == ["cancel"]
    assert item["progress"]["total_bytes_known"] is True
    assert item["progress"]["members"][0]["member_id"] == nodes[0]
    assert "phase_index" not in item["progress"]
    assert item["progress"]["checkpoint"]["digest"] == operation.plan_digest
    assert datetime.fromisoformat(item["created_at"]).tzinfo == UTC
    assert provider.get_operation(operation.operation_id)["id"] == operation.operation_id


def test_activity_provider_integrates_with_global_cursor_and_detail_projection(
    tmp_path: Path,
) -> None:
    from vonk_control.operation_api import (
        OperationProvider,
        get_operation_from_providers,
        merge_operation_providers,
        operation_detail_response,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    request = _request(sessions, nodes[0])
    operations = [
        service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(),
                request_key=str(uuid.uuid4()),
            ),
            actor="admin",
        )
        for _ in range(3)
    ]
    provider = RunSwitchOperationProvider(service)
    shared = OperationProvider(
        family=provider.family,
        list_operations=provider.list_operations,
        get_operation=provider.get_operation,
    )
    cursors = CursorCodec(hashlib.sha256(b"run-switch-activity").digest())
    first = merge_operation_providers(
        [shared],
        cursor=None,
        limit=2,
        state="queued",
        node_id=nodes[0],
        cursors=cursors,
    )
    assert len(first.items) == 2
    assert first.total == 3
    assert first.next_cursor is not None
    second = merge_operation_providers(
        [shared],
        cursor=first.next_cursor,
        limit=2,
        state="queued",
        node_id=nodes[0],
        cursors=cursors,
    )
    assert len(second.items) == 1
    assert second.total == 3
    detail = operation_detail_response(first.items[0])
    assert detail.node_ids == list(nodes)
    assert detail.progress is not None
    assert detail.progress.members[0].member_id == nodes[0]
    assert detail.recovery is not None
    assert detail.recovery.actions[0].value == "inspect"
    assert get_operation_from_providers([shared], operations[0].operation_id)["id"] == (
        operations[0].operation_id
    )


@pytest.mark.parametrize(
    "invalid_result", [[], "broken", {"phase_index": "0"}, {"phase": "old-phase"}]
)
def test_operation_read_rejects_malformed_persisted_result(
    tmp_path: Path, invalid_result: object
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
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
    with sessions.begin() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        job.result = invalid_result
        previous_state = job.state
    with pytest.raises(RunSwitchOperationConflict, match="persisted result is invalid"):
        service.get(operation.operation_id)
    with pytest.raises(RunSwitchOperationConflict, match="persisted result is invalid"):
        service.retry(operation.operation_id, request_key=str(uuid.uuid4()), actor="admin")
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        assert job.result == invalid_result
        assert job.state == previous_state


def test_terminal_checkpoint_after_retry_clears_failure_and_rejects_missing_evidence(tmp_path):
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4()))
    service = _service(sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest,
                              request_key=str(uuid.uuid4())), actor="admin",
    )
    # Resume the durable checkpoint written after the last phase of a retry.
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        row.state = "running"
        row.result = dict(row.result) | {
            "phase_index": len(plan.phases),
            "completed_phases": [phase.kind for phase in plan.phases],
            "retryable": True,
            "failed_phase": "transfer",
        }
    restarted = _service(sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor())
    assert restarted.tick() is True
    completed = restarted.get(operation.operation_id)
    assert completed.state == "succeeded"
    assert completed.result.retryable is False
    assert completed.result.failed_phase is None
    assert completed.status_reason is None
    from vonk_control.run_switch_contract import RunSwitchOperation
    with pytest.raises(ValidationError, match="completed phase evidence"):
        RunSwitchOperation.model_validate(completed.model_dump() | {"result": None})
    with pytest.raises(ValidationError, match="requires a status reason"):
        RunSwitchOperation.model_validate(completed.model_dump() | {"state": "failed"})
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        row.result = None
    with pytest.raises(ValidationError, match="completed phase evidence"):
        restarted.get(operation.operation_id)


def test_nas_transfer_checkpoint_does_not_complete_unstarted_spark_copy(tmp_path):
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            return replace(super().inspect(*args, **kwargs), missing_nas_bytes=1024, nas_coverage="partial")

    executor = ColdStartPhaseExecutor()
    service = _service(sessions, NOW, lifecycle, None, artifacts=ColdInspector(missing_spark_bytes=1024), phase_executor=executor)
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest, request_key=str(uuid.uuid4())), actor="admin")
    service.tick()
    progress = service.get(operation.operation_id).progress
    assert executor.events == ["model-download"]
    assert progress.completed_bytes == 1024
    assert progress.total_bytes > progress.completed_bytes
    assert progress.members[0].completed_bytes == 0


@pytest.mark.parametrize("child_completion", [False, True])
def test_overlapping_ticks_cannot_apply_completion_to_the_next_checkpoint(tmp_path, child_completion):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4()))

    class InterleavedExecutor(RecordingArtifactExecutor):
        entered = False
        service = None

        def execute(self, *args, **kwargs):
            result = super().execute(*args, **kwargs)
            if not child_completion and not self.entered:
                self.entered = True
                self.service.tick()
            return result

        def get(self, operation_id):
            result = super().get(operation_id)
            if child_completion and result.state == "succeeded" and not self.entered:
                self.entered = True
                self.service.tick()
            return result

    executor = InterleavedExecutor(child_transfer=child_completion)
    service = _service(sessions, NOW, lifecycle, executor, artifacts=CompleteArtifactInspector(missing_spark_bytes=1024))
    executor.service = service
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest, request_key=str(uuid.uuid4())), actor="admin")
    service.tick()
    if child_completion:
        child_id = service.get(operation.operation_id).result.child_operation_id
        child = executor.children[child_id]
        child.state = "succeeded"
        child.result = _target_copy_evidence(plan, plan.phases[0])
        service.tick()
    current = service.get(operation.operation_id)
    assert current.progress.phase_index == 1
    assert current.current_phase == "verify"
    assert current.completed_phases == ["transfer"]


def test_cancel_intent_waits_for_transfer_receipt_and_preserves_shared_copies(tmp_path):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4()))
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(sessions, NOW, lifecycle, executor, artifacts=CompleteArtifactInspector(missing_spark_bytes=1024))
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest, request_key=str(uuid.uuid4())), actor="admin")
    service.tick()
    child_id = service.get(operation.operation_id).result.child_operation_id
    key = str(uuid.uuid4())
    pending = service.cancel(operation.operation_id, actor="admin", request_key=key, reason="Stop preparation")
    assert pending.state == "running"
    assert service.cancel(operation.operation_id, actor="admin", request_key=key, reason="Stop preparation") == pending
    service.tick()
    assert service.get(operation.operation_id).state == "running"
    child = executor.children[child_id]
    child.state = "succeeded"
    child.result = _target_copy_evidence(plan, plan.phases[0])
    restarted = _service(sessions, NOW, lifecycle, executor, artifacts=CompleteArtifactInspector(missing_spark_bytes=1024))
    restarted.tick()
    cancelled = restarted.get(operation.operation_id)
    assert cancelled.state == "cancelled"
    assert cancelled.result.completed_phases == ["transfer"]
    assert cancelled.result.phase_results
    assert cancelled.result.child_operation_id is None
    assert not restarted._advance(operation.operation_id)
    with sessions() as session:
        assert len(list(session.scalars(select(NodeArtifact)))) > 0
        assert session.get(RecipeInstallation, plan.installation_id).state == "installed"


def test_cancel_queued_start_is_idempotent_but_active_runtime_requires_stop(tmp_path):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4()))
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest, request_key=str(uuid.uuid4())), actor="admin")
    key = str(uuid.uuid4())
    cancelled = service.cancel(operation.operation_id, actor="admin", request_key=key, reason="Keep the current profile")
    assert cancelled.state == "cancelled"
    assert cancelled.progress.state == "cancelled"
    with pytest.raises(RunSwitchOperationConflict, match="already used differently"):
        service.cancel(operation.operation_id, actor="admin", request_key=key, reason="Different intent")
    request_key = str(uuid.uuid4())
    active = service.apply(RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest, request_key=request_key), actor="admin")
    service.tick()
    with pytest.raises(RunSwitchOperationConflict, match="explicit Stop"):
        service.cancel(active.operation_id, actor="admin", request_key=str(uuid.uuid4()), reason="Stop running")


def test_production_build_queue_receipt_survives_phase_handoff_and_completion(tmp_path: Path) -> None:
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.recipe_builds import RecipeBuildService
    from vonk_control.recipe_operations import RecipeOperationService

    from .test_recipe_builds import setup as setup_build

    sessions, bundles, now, node_id, revision = setup_build(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    selected = builds.plan(revision.id, node_id, now=now)
    queue = AgentJobService(sessions, clock=lambda: now)
    lifecycle = RecipeOperationService(
        sessions, install_admission=object(), run_admission=object(),
        agent_jobs=queue, clock=lambda: now, builds=builds,
    )
    executor = RecipeLifecyclePhaseExecutor(
        lifecycle, sessions, ClusterMappingService(sessions), lambda: now,
    )
    # This phase consumes only the build identities already selected by preview.
    plan = SimpleNamespace(
        recipe_build_id=selected.build_id,
        recipe_revision_id=selected.recipe_revision_id,
        build=SimpleNamespace(
            build_id=selected.build_id,
            build_input_sha256=selected.build_input_sha256,
        ),
    )
    phase = RunSwitchPhase(
        index=0, kind="prepare", subphase="container-build", state="planned",
        detail="Build selected source recipe",
    )
    request_key = str(uuid.uuid4())

    def execute():
        return executor.execute(
            plan, phase, item_index=0, actor="admin", request_key=request_key,
            progress={},
        )

    scheduled = execute()
    assert scheduled.operation_id is not None
    assert lifecycle.get(scheduled.operation_id).state == "running"
    received = _phase_result(scheduled.result, phase=phase)
    assert received["state"] == "building"
    assert received["build_id"] == selected.build_id
    assert received["build_input_sha256"] == selected.build_input_sha256
    assert "image_digest" not in received
    replay = execute()
    assert replay.operation_id == scheduled.operation_id
    assert _phase_result(replay.result, phase=phase) == received

    with sessions.begin() as session:
        build = session.get(RecipeBuild, selected.build_id)
        build.state = "succeeded"
        build.image_digest = "sha256:" + "a" * 64
        build.oci_layout_sha256 = "b" * 64
        build.image_bytes = 123
        session.get(Job, scheduled.operation_id).state = "succeeded"
    completed = execute()
    assert completed.operation_id is None
    receipt = _phase_result(completed.result, phase=phase)
    assert receipt["state"] == "succeeded"
    assert receipt["image_digest"] == "sha256:" + "a" * 64
    assert receipt["oci_layout_sha256"] == "b" * 64
    assert receipt["image_bytes"] == 123
    # Success never substitutes defaults for incomplete immutable evidence.
    with sessions.begin() as session:
        session.get(RecipeBuild, selected.build_id).image_bytes = None
    with pytest.raises(RunSwitchOperationConflict, match="container-build-evidence-invalid"):
        execute()
