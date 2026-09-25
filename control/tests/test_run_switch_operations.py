from __future__ import annotations

import errno
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    RecipeReconcilePayload,
    RecipeReconcileResult,
    canonical_message,
)
from vonk_control.auth import CursorCodec
from vonk_control.cluster_mappings import ClusterMappingError, ClusterMappingService
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.install_admission import installation_plan_digest_from_stored_document
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.model_cache import (
    ArtifactSetManifest,
    ArtifactSpec,
    ModelCacheService,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
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
from vonk_control.operation_api import OperationQuery
from vonk_control.operation_contract import OperationFailureEvidence
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.recipe_operations import (
    RecipeBuildService,
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeOperationView,
)
from vonk_control.recipe_runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
)
from vonk_control.run_switch_contract import (
    InvocationMetadata,
    RunSwitchApplyRequest,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchCleanupVerifyResult,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPhaseResult,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchRetention,
    RunSwitchStartResult,
    RunSwitchTargetTransferEvidenceResult,
    RunSwitchUninstallResult,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    PhaseExecution,
    RecipeLifecyclePhaseExecutor,
    RunSwitchIssuedWorkloadPending,
    RunSwitchOperationConflict,
    RunSwitchOperationProvider,
    RunSwitchOperationService,
    _phase_result,
    _transient_distribution_exception,
    effective_build_receipt,
)
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    prepare_runtime_image,
)
from vonk_control.runtime_preflight import latest_result

from .preflight_fixtures import record_passing_preflight

_FIXTURE_ADAPTER = resolve_runtime_adapter("vllm", {"mode": "single"})
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
        (
            "stop",
            None,
            {"phase": "start", "run_id": "11111111-1111-4111-8111-111111111111"},
        ),
        (
            "prepare",
            "runtime-image",
            {"phase": "prepare", "subphase": "runtime-plan", "prepared": True},
        ),
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


def _result(view: RunSwitchOperation) -> RunSwitchOperationResult:
    """Return the durable result a started Run/Switch operation recorded."""

    assert view.result is not None
    return view.result


def _child_operation_id(view: RunSwitchOperation) -> str:
    """Return the durable child operation a waiting view is bound to."""

    child_id = _result(view).child_operation_id
    assert child_id is not None
    return child_id


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
        "verified_digests": list(getattr(plan.storage, "artifact_digests", ()))
        or [MODEL_ARTIFACT],
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
    image = (
        image
        or getattr(plan.build, "image_digest", None)
        or getattr(plan, "image_digest", None)
    )
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
        "runtime_adapter": _FIXTURE_ADAPTER.adapter_id,
        "runtime_adapter_sha256": _FIXTURE_ADAPTER.digest,
    }


class CompleteArtifactInspector:
    def __init__(
        self, *, reclaimable_bytes: int = 0, missing_spark_bytes: int = 0
    ) -> None:
        self.reclaimable_bytes = reclaimable_bytes
        self.missing_spark_bytes = missing_spark_bytes

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        del session, model_content_sha256, recipe_revision_id, retention, now
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


class ModelCacheManifestProvider(ModelCacheService):
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
    def __init__(
        self, *, child_transfer: bool = False, bad_verify: bool = False
    ) -> None:
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
            runtime_image = getattr(
                getattr(plan, "preparation", None), "runtime_image", None
            )
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
            installation_id = getattr(plan, "installation_id", None) or str(
                uuid.uuid4()
            )
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

    def get(self, operation_id: str):
        raise KeyError(operation_id)


def _request(
    sessions,
    node_id: str,
    *,
    action: Literal["run", "switch"] = "run",
    retention: RunSwitchRetention = "retain-cached",
):
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


def test_same_clock_later_intent_fences_older_queued_work(tmp_path: Path) -> None:
    """Database admission order, not timestamp spelling, owns the node."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    first = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    second = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions() as session:
        old = session.get(Job, first.operation_id)
        new = session.get(Job, second.operation_id)
        assert old is not None and new is not None
        assert old.created_at == new.created_at
        assert old.payload["workload_intent_ordinal"] == 1
        assert new.payload["workload_intent_ordinal"] == 2
    assert service._advance(first.operation_id) is True
    assert service.get(first.operation_id).state == "cancelled"
    assert "superseded" in (service.get(first.operation_id).status_reason or "")
    assert service.get(second.operation_id).state == "queued"


def test_child_activity_change_persists_without_clock_only_writes(
    tmp_path: Path,
) -> None:
    """An unchanged poll is quiet, but a changed stall signal is durable."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    for _ in range(8):
        assert service.tick() is True
        if _result(service.get(operation.operation_id)).child_operation_id is not None:
            break
    child = executor.children[_child_operation_id(service.get(operation.operation_id))]
    child.result = {
        "operation": {
            "phase": "transfer",
            "completed_bytes": 0,
            "total_bytes_known": False,
            "activity": "active",
            "observed_at": NOW.isoformat(),
        }
    }
    assert service.tick() is True
    before = _result(service.get(operation.operation_id)).operation
    assert before is not None and before.activity == "active"
    child.result["operation"]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert service.tick() is False
    child.result["operation"]["activity"] = "waiting"
    assert service.tick() is True
    after = _result(service.get(operation.operation_id)).operation
    assert after is not None and after.activity == "waiting"


def test_due_scheduler_reaches_work_past_a_full_parked_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sixteen parked scopes must not starve a seventeenth due scope."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    due_id = str(uuid.uuid4())
    with sessions.begin() as session:
        for index in range(17):
            job_id = f"00000000-0000-4000-8000-{index:012x}"
            session.add(
                Job(
                    id=job_id,
                    request_id=str(uuid.uuid4()),
                    kind="recipe.run-switch.v2",
                    state="running",
                    actor="admin",
                    authority_revision="a" * 64,
                    targets=[f"spk_{index:032x}"],
                    payload_digest="a" * 64,
                    payload={},
                    result={
                        "observation_due_at": (
                            NOW if index == 16 else NOW + timedelta(minutes=1)
                        )
                        .isoformat()
                        .replace("+00:00", "Z")
                    },
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            if index == 16:
                due_id = job_id
    seen: list[str] = []
    monkeypatch.setattr(service, "_advance", lambda job_id: seen.append(job_id) or True)
    assert service.tick() is True
    assert seen == [due_id]
    assert service.tick() is True
    assert seen == [due_id, due_id]


@pytest.mark.parametrize("damage", ["plan", "result"])
def test_malformed_operation_is_rejected_without_aborting_the_batch(
    tmp_path: Path,
    damage: str,
) -> None:
    """One invalid persisted contract must not deny a valid operation its turn."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(child_transfer=True),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    valid = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    malformed_id = "00000000-0000-4000-8000-000000000000"
    assert malformed_id < valid.operation_id
    with sessions.begin() as session:
        session.add(
            Job(
                id=malformed_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="running",
                actor="admin",
                authority_revision="a" * 64,
                targets=[nodes[0]],
                payload_digest="a" * 64,
                payload=(
                    {"plan": {"not": "a plan"}}
                    if damage == "plan"
                    else {"plan": plan.model_dump(mode="json")}
                ),
                result=["malformed"] if damage == "result" else None,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with sessions() as session:
        stored_valid = session.get(Job, valid.operation_id)
        assert stored_valid is not None
        before_index = (stored_valid.result or {}).get("phase_index")

    assert service.tick() is True

    with sessions() as session:
        rejected = session.get(Job, malformed_id)
        assert rejected is not None
        assert rejected.state == "failed"
        assert rejected.status_reason == (
            "run-switch persisted plan is invalid"
            if damage == "plan"
            else "run-switch persisted progress is invalid"
        )
        # The invalid evidence is retained rather than rewritten into a valid
        # contract or a claim that issued effects stopped.
        if damage == "result":
            assert rejected.result == ["malformed"]
        advanced = session.get(Job, valid.operation_id)
        assert advanced is not None and advanced.state == "running"
        after_index = (advanced.result or {}).get("phase_index")
    # The unrelated due operation still advanced in the same batch.
    assert after_index == 1
    assert after_index != before_index


def test_default_run_switch_admission_uses_the_recipe_memory_reserve(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed, plan.blockers
    assert any(
        reason.code == "run-switch.resource.estimate_uncertain"
        and reason.severity == "warning"
        and "declared recipe-role memory envelope" in reason.detail
        for reason in plan.warnings
    )
    node = plan.fit_current.nodes[0]
    assert node.resource_demand is not None
    assert node.resource_demand.total_bytes == node.memory_required_bytes


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
        # Persisted JSON can be an array; the mapper must reject it rather than
        # treat it as an empty parameter map.
        session.execute(
            update(ClusterMapping)
            .where(ClusterMapping.id == mapping_id)
            .values(parameters=["malformed"])
        )

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


def test_fresh_unmapped_group_uses_default_mapping_and_install_composite(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        for item in session.scalars(
            select(ClusterMappingNode).where(
                ClusterMappingNode.mapping_id == mapping_id
            )
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
    # NAS readiness must not wait for copies on the Sparks. The preparation
    # below is exactly the one a profile preview and later apply will consume.
    assert plan.preparation.controller_ready is True
    assert plan.preparation.targets_ready is False
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
    assert (
        service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(),
                plan_digest=plan.plan_digest,
                request_key=operation.request_key,
            ),
            actor="admin",
        ).operation_id
        == operation.operation_id
    )

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
            select(Job)
            .where(Job.kind == "recipe.install")
            .order_by(Job.created_at.desc())
        )
    assert created is not None
    assert child is not None


def test_model_cache_manifest_allows_planned_nas_download(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
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
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
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

    controller_storage = FilesystemRuntimeImageStorage(
        tmp_path / "controller-artifacts"
    )
    expected_archive = b"canonical-runtime-image-archive"[:image_bytes]
    assert hashlib.sha256(expected_archive).hexdigest() == layout_digest
    (controller_storage.root / layout_digest).write_bytes(expected_archive)

    class ColdModelCache(_CanonicalModelCache):
        ready = False

        def verified_model_objects_for_set(self, artifact_set_sha256):
            if not self.ready:
                raise RuntimeError("model bytes are not verified yet")
            return super().verified_model_objects_for_set(artifact_set_sha256)

    model_cache = ColdModelCache()

    class _BuildArchiveTransport:
        def pull_and_export(
            self,
            reference: str,
            destination: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            progress=None,
        ) -> PulledImageEvidence:
            raise AssertionError("the test runtime image archive is already stored")

        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            assert archive.read_bytes() == expected_archive
            return PulledImageEvidence(
                manifest_digest=image_digest,
                requested_manifest_digest=None,
                config_id="sha256:" + "4" * 64,
                local_reference="oci-archive:" + str(archive),
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
    assert (
        controller_storage.find_verified(
            image_digest,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is not None
    )

    # Controller compilation/persistence is synchronous and must not create a
    # Spark install child before target-copy verification.
    assert service.tick() is True
    planned = service.get(operation.operation_id)
    assert planned.progress.subphase == "target-copy"
    assert planned.result is not None
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
        assert isinstance(compiled_plans, dict)
        assert compiled_plans
        assert all(
            payload["schema_version"] == 2 for payload in compiled_plans.values()
        )
    assert executor.events == ["model-download", "runtime-image", "runtime-plan"]

    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase == "target-copy"
    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase == "runtime-install"
    install_stage = service.get(operation.operation_id)
    assert install_stage.result is not None
    assert install_stage.result.child_operation_id is None
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.progress.subphase == "runtime-install"
    assert waiting.result is not None
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

    def __init__(
        self, delegate, clock, seconds, during_compile, slow_compiles=1
    ) -> None:
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
                    memory_pool="shared",
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
        """Advance one due phase, answering any ordinary preflight probe."""
        before = service.get(operation.operation_id)
        due = before.result.observation_due_at if before.result is not None else None
        if due is not None and due > clock.now:
            clock.now = due
        service.tick()
        view = service.get(operation.operation_id)
        assert view.result is not None
        checkpoint = view.result.preflight
        if checkpoint is not None and checkpoint.pending_job_id:
            with sessions() as session:
                pending = session.get(Job, checkpoint.pending_job_id)
                needs_result = pending is not None and pending.state in {
                    "queued",
                    "running",
                }
            if needs_result:
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
    assert (
        failure.result.retry_reason
        == "runtime preflight expired during install compilation"
    )
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


def test_preflight_refresh_after_repeated_cold_compiles_recovers_exact_plan(
    tmp_path: Path,
) -> None:
    """Freshness expiry backs off without abandoning accepted exact intent."""
    switch = _cold_compile_switch(tmp_path, slow_compiles=99)
    service, operation = switch.service, switch.operation
    with switch.sessions() as session:
        original = dict(session.get(Job, operation.operation_id).payload)
    # A phase may compile more than once while refreshing bound build evidence.
    # Keep the fault present until six durable failures have actually occurred,
    # independent of the number of compiler calls made by each phase attempt.
    for _ in range(60):
        view = service.get(operation.operation_id)
        assert view.state in {"queued", "running"}, view.status_reason
        if (_result(view).retry_attempt or 1) >= 7:
            break
        assert "runtime-install" not in switch.executor.events
        switch.drive()
        held = service.get(operation.operation_id)
        if (
            held.result.retry_reason is not None
            and held.result.observation_due_at is not None
        ):
            assert held.result.observation_due_at <= switch.clock.now + timedelta(
                seconds=60
            )
            if held.result.observation_due_at > switch.clock.now:
                assert service.tick() is False
    else:
        pytest.fail("cold compilation never exercised six failed recovery cycles")
    assert _result(view).retry_attempt == 7
    with switch.sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
    switch.compiler._slow_compiles = 0
    for _ in range(20):
        switch.drive()
        if "runtime-install" in switch.executor.events:
            break
    assert switch.executor.events.count("runtime-install") == 1
    with switch.sessions() as session:
        assert session.get(Job, operation.operation_id).payload == original
        installations = list(session.scalars(select(RecipeInstallation)))
        assert len(installations) == 1
        assert installations[0].plan_digest == switch.admitted.plan_digest


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


def test_preflight_receipt_disagreement_backs_off_then_recovers(
    tmp_path: Path,
) -> None:
    """Receipt ordering races wait durably without replaying completed phases."""
    switch = _cold_compile_switch(tmp_path, seconds=1, slow_compiles=99)
    stale_time = switch.clock.now - timedelta(seconds=301)
    record_passing_preflight(switch.sessions, stale_time)
    with switch.sessions() as session:
        stale_ids = list(
            session.scalars(
                select(AgentOperation.id).where(
                    AgentOperation.kind == "runtime.preflight.v1",
                    AgentOperation.created_at == stale_time,
                )
            )
        )
    assert stale_ids

    def order_stale_receipts(*, latest: bool) -> None:
        with switch.sessions.begin() as session:
            for operation_id in stale_ids:
                operation = session.get(AgentOperation, operation_id)
                operation.updated_at = (
                    switch.clock.now + timedelta(seconds=1) if latest else stale_time
                )

    switch.hooks.append(lambda: order_stale_receipts(latest=True))
    for _ in range(40):
        view = switch.service.get(switch.operation.operation_id)
        assert view.state in {"queued", "running"}, view.status_reason
        if (_result(view).retry_attempt or 1) >= 5:
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
                assert selected is not None
                assert selected.observed_at == int(stale_time.timestamp()), (
                    view.state,
                    view.status_reason,
                    switch.executor.events,
                    switch.compiler.compiles,
                    receipt.request_sha256,
                )

    view = switch.service.get(switch.operation.operation_id)
    assert view.state == "running"
    assert view.result.observation_due_at > switch.clock.now
    assert switch.service.tick() is False
    # Count committed failed cycles, not compiler calls: refreshing an exact
    # build receipt may compile more than once inside one phase attempt.
    assert _result(view).retry_attempt == 5
    assert "runtime-install" not in switch.executor.events
    with switch.sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []
    switch.hooks.clear()
    order_stale_receipts(latest=False)
    for _ in range(10):
        switch.drive()
        if "runtime-install" in switch.executor.events:
            break
    assert switch.executor.events.count("runtime-install") == 1


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
            result=None,
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
    # Polling an unchanged build child does not rewrite durable progress.
    assert restarted.tick() is False
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
    assert resumed.result is not None
    receipt = effective_build_receipt(plan, resumed.result.model_dump(mode="json"))
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
        node.binary_digest = "a" * 64
        node.capabilities = [*node.capabilities, "recipe.build.v1"]
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


def test_editorial_successor_reuses_an_identity_matched_build(tmp_path: Path) -> None:
    """A notes-only successor reuses the identical prepared build.

    ``persist_plan_in_session`` deliberately reuses a succeeded receipt across a
    revision boundary when the executable input identity matches, keeping the
    older row and its revision id.  The build selection used to reject that row
    because its recorded revision id no longer matched the head, so an editorial
    rename planned a full rebuild of identical bytes.
    """

    import copy

    from vonk_forge_contracts import RecipeDefinition, content_sha256

    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.binary_digest = "a" * 64
        node.capabilities = [*node.capabilities, "recipe.build.v1"]
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
        metadata = document["metadata"]
        assert isinstance(metadata, dict)
        metadata["title"] = "Editorially renamed recipe"
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
            content_digest=content_sha256(canonical),
            artifact_key="b" * 64,
            execution_key="c" * 64,
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
            recipe_content_sha256=content_sha256(canonical),
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
            retention="retain-cached",
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
            memory_pool="shared",
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
            result=None,
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
    progress = parent.result.model_dump(mode="json")
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
    assert execution.result == {
        "build_id": build_id,
        "build_input_sha256": "e" * 64,
        "state": "building",
        "phase": "prepare",
        "subphase": "container-build",
    }
    assert execution.result is not None
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
            request_key=request_key,
            progress=progress,
        )


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
    assert "run-switch.resource.insufficient_reservation_budget" in {
        reason.code for reason in plan.fit_current.blockers
    }
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
    uncertain_reason = next(
        reason
        for reason in current_node.blockers
        if reason.code == "run-switch.resource.resident_usage_unknown"
    )
    assert "Capacity is unverified" in uncertain_reason.detail
    assert "0..7800 bytes" in uncertain_reason.detail
    assert "leaves -" not in uncertain_reason.detail
    round_tripped = RunSwitchPlan.model_validate_json(plan.model_dump_json())
    assert round_tripped.fit_current.nodes[0].memory_usage_uncertainty == uncertainty
    bad_stop = plan.model_dump(mode="python")
    bad_stop["post_stop_memory_check"] = {"stop_run_ids": [str(uuid.uuid4())]}
    with pytest.raises(ValidationError, match="exact reviewed stops"):
        RunSwitchPlan.model_validate(bad_stop)
    impossible_capacity = plan.model_dump(mode="python")
    fit_node = impossible_capacity["fit_current"]["nodes"][0]
    fit_node["memory_capacity_bytes"] = (
        fit_node["memory_required_bytes"] + fit_node["memory_floor_bytes"] - 1
    )
    with pytest.raises(ValidationError, match="known feasible demand and capacity"):
        RunSwitchPlan.model_validate(impossible_capacity)
    assert [phase.kind for phase in plan.phases] == [
        "stop",
        "prepare",
        "prepare",
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
    assert physically_impossible.post_stop_memory_check is None
    assert not physically_impossible.allowed
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
    assert "run-switch.resource.insufficient_reservation_budget" in {
        reason.code for reason in changed.blockers
    }


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
    assert "run.insufficient_memory" not in codes
    assert "run.port_occupied" not in codes
    assert "run.rendezvous_port_occupied" not in codes
    assert "run-switch.run_admission_blocked" not in codes
    assert plan.allowed is True
    assert [stop.run_id for stop in plan.stops] == [run_id]


def test_artifact_child_checkpoint_and_digest_mismatch_fail_closed(
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
    artifact_executor.children[child_id].state = "succeeded"
    artifact_executor.children[child_id].result = _target_copy_evidence(
        plan, plan.phases[0]
    )
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
    child_id = _child_operation_id(service.get(operation.operation_id))
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
    assert plan.preparation is not None
    runtime_image = plan.preparation.runtime_image
    assert runtime_image is not None
    artifact_executor.children[child_id].result = {
        "copied_bytes": 1024,
        "evidence": [
            {
                "node_id": nodes[0],
                "verified": True,
                "verified_digests": [MODEL_ARTIFACT],
                "verified_image_digest": "sha256:" + "1" * 64,
                "imported_image_digest": "sha256:" + "1" * 64,
                "verified_oci_layout_sha256": runtime_image.oci_layout_sha256,
            }
        ],
    }
    assert restarted.tick() is True
    completed_transfer = restarted.get(operation.operation_id)
    assert completed_transfer.progress.completed_bytes == 1024
    assert completed_transfer.progress.members[0].completed_bytes == 1024
    assert completed_transfer.progress.members[0].state == "succeeded"
    assert any(
        isinstance(item, RunSwitchTargetTransferEvidenceResult)
        and item.node_id == nodes[0]
        and item.verified is True
        for item in _result(completed_transfer).phase_results
    )
    # The next durable tick consumes the persisted transfer receipts and runs
    # the real verify phase; no caller supplied progress is reconstructed.
    assert restarted.tick() is True
    verified = restarted.get(operation.operation_id)
    assert "verify" in _result(verified).completed_phases


def test_transient_distribution_child_is_not_replayed_by_parent(
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
    child_id = _child_operation_id(service.get(operation.operation_id))
    artifact_executor.children[child_id].state = "failed"
    artifact_executor.children[child_id].result = {
        "error_code": "agent.copy.timeout",
        "failure_kind": "temporary-dependency",
        "progress": {
            "completed_bytes": 512,
            "total_bytes": 1024,
            "members": [
                {
                    "node_id": nodes[0],
                    "state": "unknown",
                    "completed_bytes": 512,
                    "total_bytes": 1024,
                }
            ],
        },
    }
    assert service.tick() is True
    failed = service.get(operation.operation_id)
    assert failed.state == "failed"
    assert failed.plan_digest == plan.plan_digest
    assert failed.progress.completed_bytes == 512
    assert failed.result is not None and failed.result.retryable
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        assert row.current_attempt <= 1
        assert row.result is not None
        assert row.result["child_operation_id"] == child_id
    assert service.tick() is False


def test_operator_retry_uses_a_new_request_after_typed_transient_failure(
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
    child_id = _child_operation_id(service.get(operation.operation_id))
    artifact_executor.children[child_id].state = "failed"
    artifact_executor.children[child_id].result = {
        "error_code": "agent.copy.timeout",
        "failure_kind": "temporary-dependency",
    }
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
        retry_payload = row.payload["retry"]
        assert isinstance(retry_payload, dict)
        assert retry_payload["operator_retries"] == 1

    assert service.tick() is True
    retried_child = _child_operation_id(service.get(retry.operation_id))
    artifact_executor.children[retried_child].state = "succeeded"
    assert plan.preparation is not None
    retried_image = plan.preparation.runtime_image
    assert retried_image is not None
    artifact_executor.children[retried_child].result = {
        "copied_bytes": 1024,
        "evidence": [
            {
                "node_id": nodes[0],
                "verified": True,
                "verified_digests": [MODEL_ARTIFACT],
                "verified_image_digest": "sha256:" + "1" * 64,
                "imported_image_digest": "sha256:" + "1" * 64,
                "verified_oci_layout_sha256": retried_image.oci_layout_sha256,
            }
        ],
    }
    assert service.tick() is True


def test_run_switch_retry_classification_rejects_terminal_http_and_storage_errors() -> (
    None
):
    request = httpx.Request("GET", "https://example.invalid/artifact")
    for status in (401, 403, 404):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError(
            "request failed", request=request, response=response
        )
        assert _transient_distribution_exception(error) is False
    for status in (429, 500, 503):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError(
            "request failed", request=request, response=response
        )
        assert _transient_distribution_exception(error) is True
    assert (
        _transient_distribution_exception(OSError(errno.EPERM, "permission denied"))
        is False
    )
    assert (
        _transient_distribution_exception(OSError(errno.ENOSPC, "no space left"))
        is False
    )
    assert _transient_distribution_exception(OSError(errno.ECONNRESET, "reset")) is True


class _CountingPreviewLifecycle(RecipeOperationService):
    """Real lifecycle service that counts how often admission is re-derived.

    It replaces no production state: every other call is delegated to the real
    service, so the count observes the executor's actual admission decision.
    """

    def __init__(self, inner: RecipeOperationService) -> None:
        self._inner = inner
        self.previews = 0

    def preview_run(
        self,
        installation_id: str,
        alias: str,
        *,
        profile_application_id: str | None = None,
    ):
        self.previews += 1
        return self._inner.preview_run(
            installation_id, alias, profile_application_id=profile_application_id
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_start_phase_adopts_the_child_it_already_queued(tmp_path: Path) -> None:
    """A resumed start phase keeps the durable child it already queued.

    Run admission hashes live inventory observation time and current
    reservations, so re-previewing after the first start is admitted derives a
    different plan digest for the identical child.  Without adoption the
    executor offered the unchanged request key to admission with the changed
    digest, and admission correctly rejected the second attempt instead of
    resuming the child that was already running.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    start_phase = next(phase for phase in plan.phases if phase.kind == "start")
    counting = _CountingPreviewLifecycle(lifecycle)
    executor = RecipeLifecyclePhaseExecutor(
        counting, sessions, ClusterMappingService(sessions), lifecycle._clock()
    )
    with sessions.begin() as session:
        for node_id in nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 1
    progress = {
        "workload_intent_ordinal": 1,
        "phase_results": [{"installation_id": installation.owner_id}],
    }
    request_key = str(uuid.uuid4())

    first = executor.execute(
        plan,
        start_phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress=progress,
    )
    second = executor.execute(
        plan,
        start_phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress=progress,
    )

    assert first.operation_id is not None
    assert second.operation_id == first.operation_id
    # The digest was never re-derived: adoption bound the durable request key,
    # operation kind, owner kind, installation and alias instead.
    assert counting.previews == 1
    with sessions() as session:
        starts = tuple(session.scalars(select(Job).where(Job.kind == "recipe.start")))
    assert len(starts) == 1


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
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
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
    assert (
        service.preview(web_request, actor="admin").plan_digest
        == service.preview(cli_request, actor="admin").plan_digest
    )


def test_activity_provider_preserves_group_and_canonical_nested_progress(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
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
        OperationQuery(after=None, limit=10, state=None, node_id=None)
    )
    assert page.total == 1
    item = page.items[0]
    assert isinstance(item, dict)
    progress = item["progress"]
    assert isinstance(progress, dict)
    checkpoint = progress["checkpoint"]
    assert isinstance(checkpoint, dict)
    attempt = item["attempt"]
    assert isinstance(attempt, int)
    created_at = item["created_at"]
    assert isinstance(created_at, str)
    assert item["id"] == operation.operation_id
    assert item["job_id"] == operation.operation_id
    assert item["node_ids"] == list(nodes)
    assert item["node_id"] == nodes[0]
    assert attempt >= 1
    assert item["supported_actions"] == ["cancel"]
    assert progress["total_bytes_known"] is True
    assert progress["members"][0]["member_id"] == nodes[0]
    assert "phase_index" not in progress
    assert checkpoint["digest"] == operation.plan_digest
    assert datetime.fromisoformat(created_at).tzinfo == UTC
    assert (
        provider.get_operation(operation.operation_id)["id"] == operation.operation_id
    )


def test_activity_provider_integrates_with_global_cursor_and_detail_projection(
    tmp_path: Path,
) -> None:
    from vonk_control.operation_api import (
        OperationProvider,
        get_operation_from_providers,
        merge_operation_providers,
        operation_detail_response,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
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


def test_activity_provider_keeps_valid_items_when_one_plan_is_unreadable(
    tmp_path: Path,
) -> None:
    from vonk_control.operation_api import (
        OperationProvider,
        merge_operation_providers,
        operation_detail_response,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    operations = [
        service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(), request_key=str(uuid.uuid4())
            ),
            actor="admin",
        )
        for _ in range(2)
    ]
    valid, unreadable = operations
    with sessions.begin() as session:
        job = session.get(Job, unreadable.operation_id)
        assert job is not None and isinstance(job.payload, dict)
        persisted_payload = dict(job.payload)
        persisted_plan = persisted_payload.get("plan")
        assert isinstance(persisted_plan, dict)
        invalid_plan = dict(persisted_plan)
        # A required current-contract field is absent. Reading the durable
        # operation must stay strict; Activity will report this row as invalid.
        invalid_plan.pop("action")
        persisted_payload["plan"] = invalid_plan
        job.payload = persisted_payload
        job.created_at = job.created_at + timedelta(seconds=1)
        job.updated_at = job.created_at

    with pytest.raises(RunSwitchOperationConflict, match="persisted plan is invalid"):
        service.get(unreadable.operation_id)

    provider = service.activity_provider()
    shared_provider = OperationProvider(
        family=provider.family,
        list_operations=provider.list_operations,
        get_operation=provider.get_operation,
    )
    page = merge_operation_providers(
        [shared_provider],
        cursor=None,
        limit=10,
        state=None,
        node_id=nodes[0],
        cursors=CursorCodec(hashlib.sha256(b"run-switch-unreadable").digest()),
    )

    assert page.total == 2
    assert [item["id"] for item in page.items] == [
        unreadable.operation_id,
        valid.operation_id,
    ]
    damaged_item = page.items[0]
    assert damaged_item["kind"] == "run-switch-unreadable"
    assert damaged_item["state"] == "unavailable"
    assert damaged_item["failure"] == {
        "error_code": "operation_history_unreadable",
        "summary": "Stored Run/Switch history is malformed",
        "retryable": False,
    }
    damaged_detail = operation_detail_response(damaged_item)
    assert damaged_detail.id == unreadable.operation_id
    assert isinstance(damaged_detail.failure, OperationFailureEvidence)
    assert damaged_detail.failure.error_code == "operation_history_unreadable"
    assert page.items[1]["id"] == valid.operation_id
    assert page.items[1]["node_ids"] == list(nodes)
    assert page.items[1].get("failure") is None


@pytest.mark.parametrize("failed", [False, True])
def test_measured_operation_keeps_unknown_totals_and_failure_readable(
    tmp_path: Path, failed: bool
) -> None:
    from vonk_control.operation_api import operation_detail_response
    from vonk_control.operation_contract import OperationFailureEvidence

    switch = _cold_compile_switch(tmp_path)
    switch.drive()
    reason = (
        "install plan is blocked: " + "missing durable runtime image receipt; " * 10
    )
    if failed:
        switch.service._fail(switch.operation.operation_id, reason)
    operation = switch.service.get(switch.operation.operation_id)
    detail = operation_detail_response(
        switch.service.activity_provider().get_operation(operation.operation_id)
    )
    assert detail.state == ("failed" if failed else "running")
    assert detail.progress is not None
    assert detail.progress.total_bytes is None
    assert detail.progress.total_bytes_known is False
    if failed:
        assert isinstance(detail.failure, OperationFailureEvidence)
        assert detail.failure.detail == reason
    else:
        assert detail.failure is None


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
        # A stored result can be any JSON value; persist it the way the driver
        # stored it rather than through the typed ORM attribute.
        session.execute(
            update(Job)
            .where(Job.id == operation.operation_id)
            .values(result=invalid_result)
        )
        previous_state = job.state
    with pytest.raises(RunSwitchOperationConflict, match="persisted result is invalid"):
        service.get(operation.operation_id)
    with pytest.raises(RunSwitchOperationConflict, match="persisted result is invalid"):
        service.retry(
            operation.operation_id, request_key=str(uuid.uuid4()), actor="admin"
        )
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        assert job.result == invalid_result
        assert job.state == previous_state


def test_terminal_checkpoint_after_retry_clears_failure_and_rejects_missing_evidence(
    tmp_path,
):
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
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
    # Resume the durable checkpoint written after the last phase of a retry.
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.state = "running"
        assert row.result is not None
        row.result = row.result | {
            "phase_index": len(plan.phases),
            "completed_phases": [phase.kind for phase in plan.phases],
            "retryable": True,
            "failed_phase": "transfer",
        }
    restarted = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    assert restarted.tick() is True
    completed = restarted.get(operation.operation_id)
    assert completed.state == "succeeded"
    assert completed.result is not None
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
        assert row is not None
        row.result = None
    with pytest.raises(ValidationError, match="completed phase evidence"):
        restarted.get(operation.operation_id)


def test_nas_transfer_checkpoint_does_not_complete_unstarted_spark_copy(tmp_path):
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            return replace(
                super().inspect(*args, **kwargs),
                missing_nas_bytes=1024,
                nas_coverage="partial",
            )

    executor = ColdStartPhaseExecutor()
    service = _service(
        sessions,
        NOW,
        lifecycle,
        None,
        artifacts=ColdInspector(missing_spark_bytes=1024),
        phase_executor=executor,
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
    service.tick()
    progress = service.get(operation.operation_id).progress
    assert executor.events == ["model-download"]
    assert progress.completed_bytes == 1024
    total_bytes = progress.total_bytes
    assert total_bytes is not None
    assert total_bytes > progress.completed_bytes
    assert progress.members[0].completed_bytes == 0


@pytest.mark.parametrize("child_completion", [False, True])
def test_overlapping_ticks_cannot_apply_completion_to_the_next_checkpoint(
    tmp_path, child_completion
):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )

    class InterleavedExecutor(RecordingArtifactExecutor):
        entered = False
        service: RunSwitchOperationService | None = None

        def execute(self, *args, **kwargs):
            result = super().execute(*args, **kwargs)
            if not child_completion and not self.entered:
                self.entered = True
                assert self.service is not None
                self.service.tick()
            return result

        def get(self, operation_id):
            result = super().get(operation_id)
            assert result is not None
            if child_completion and result.state == "succeeded" and not self.entered:
                self.entered = True
                assert self.service is not None
                self.service.tick()
            return result

    executor = InterleavedExecutor(child_transfer=child_completion)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    executor.service = service
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
    service.tick()
    if child_completion:
        child_id = _child_operation_id(service.get(operation.operation_id))
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
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
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
    service.tick()
    child_id = _child_operation_id(service.get(operation.operation_id))
    key = str(uuid.uuid4())
    pending = service.cancel(
        operation.operation_id,
        actor="admin",
        request_key=key,
        reason="Stop preparation",
    )
    assert pending.state == "running"
    assert (
        service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=key,
            reason="Stop preparation",
        )
        == pending
    )
    service.tick()
    assert service.get(operation.operation_id).state == "running"
    child = executor.children[child_id]
    child.state = "succeeded"
    child.result = _target_copy_evidence(plan, plan.phases[0])
    restarted = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    restarted.tick()
    cancelled = restarted.get(operation.operation_id)
    assert cancelled.state == "cancelled"
    cancelled_result = _result(cancelled)
    assert cancelled_result.completed_phases == ["transfer"]
    assert cancelled_result.phase_results
    assert cancelled_result.child_operation_id is None
    assert not restarted._advance(operation.operation_id)
    with sessions() as session:
        assert len(list(session.scalars(select(NodeArtifact)))) > 0
        installed = session.get(RecipeInstallation, plan.installation_id)
        assert installed is not None
        assert installed.state == "installed"


def test_cancel_queued_start_is_idempotent_but_active_runtime_requires_stop(tmp_path):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
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
    key = str(uuid.uuid4())
    cancelled = service.cancel(
        operation.operation_id,
        actor="admin",
        request_key=key,
        reason="Keep the current profile",
    )
    assert cancelled.state == "cancelled"
    assert cancelled.progress.state == "cancelled"
    with pytest.raises(RunSwitchOperationConflict, match="already used differently"):
        service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=key,
            reason="Different intent",
        )
    request_key = str(uuid.uuid4())
    active = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=request_key,
        ),
        actor="admin",
    )
    service.tick()
    with pytest.raises(RunSwitchOperationConflict, match="explicit Stop"):
        service.cancel(
            active.operation_id,
            actor="admin",
            request_key=str(uuid.uuid4()),
            reason="Stop running",
        )


def test_production_build_queue_receipt_survives_phase_handoff_and_completion(
    tmp_path: Path,
    postgres_engine,
) -> None:
    from .test_run_switch_build_fencing import _direct_parent

    sessions, planner, parent, request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    executor = planner._phase_executor
    assert lifecycle is not None and executor is not None
    with sessions() as session:
        job = session.get(Job, parent.operation_id)
        assert job is not None
        plan = RunSwitchPlan.model_validate_json(json.dumps(job.payload["plan"]))
        progress = dict(job.result or {})
    phase = plan.phases[0]
    assert phase.subphase == "container-build"
    request_key = request.request_key
    assert request_key is not None

    def execute():
        return executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )

    scheduled = execute()
    assert scheduled.operation_id is not None
    assert scheduled.result is not None
    assert lifecycle.get(scheduled.operation_id).state == "running"
    received = _phase_result(scheduled.result, phase=phase)
    assert received["state"] == "building"
    assert received["build_id"] == selected.build_id
    assert received["build_input_sha256"] == selected.build_input_sha256
    assert "image_digest" not in received
    replay = execute()
    assert replay.operation_id == scheduled.operation_id
    assert replay.result is not None
    assert _phase_result(replay.result, phase=phase) == received

    with sessions.begin() as session:
        build = session.get(RecipeBuild, selected.build_id)
        assert build is not None
        build.state = "succeeded"
        build.image_digest = "sha256:" + "a" * 64
        build.oci_layout_sha256 = "b" * 64
        build.image_bytes = 123
        job = session.get(Job, scheduled.operation_id)
        assert job is not None
        job.state = "succeeded"
    completed = execute()
    assert completed.operation_id is None
    assert completed.result is not None
    receipt = _phase_result(completed.result, phase=phase)
    assert receipt["state"] == "succeeded"
    assert receipt["image_digest"] == "sha256:" + "a" * 64
    assert receipt["oci_layout_sha256"] == "b" * 64
    assert receipt["image_bytes"] == 123
    # Success never substitutes defaults for incomplete immutable evidence.
    with sessions.begin() as session:
        stale = session.get(RecipeBuild, selected.build_id)
        assert stale is not None
        stale.image_bytes = None
    with pytest.raises(
        RunSwitchOperationConflict, match="container-build-evidence-invalid"
    ):
        execute()


class _ObservingLifecycle(RecipeOperationService):
    """Real lifecycle service with one scripted run observation.

    Everything except the observation delegates to the real service, so the
    decision under review sees real durable children and real plans.
    """

    def __init__(self, inner: RecipeOperationService, *, healthy: bool) -> None:
        self._inner = inner
        self._healthy = healthy

    def run_status(self, run_id: str):
        return replace(
            self._inner.run_status(run_id),
            healthy=self._healthy,
            route_state="published" if self._healthy else "withdrawn",
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _parked_start_switch(tmp_path: Path, *, healthy: bool):
    """A real Run/Switch checkpointed at a parked start whose run is observed."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    observing = _ObservingLifecycle(lifecycle, healthy=healthy)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=observing,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
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
    start_index = next(
        index for index, phase in enumerate(plan.phases) if phase.kind == "start"
    )
    # The launch happens after admission, exactly as the real start phase would
    # have done it, and is then left parked by the interrupted agent.
    assert operation.result is not None
    ordinal = operation.result.workload_intent_ordinal
    assert ordinal is not None
    run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
    started = lifecycle.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
        workload_intent_ordinal=ordinal,
    )
    with sessions.begin() as session:
        # The launch was interrupted: its operation is parked while the run it
        # produced may still be serving.
        child = session.get(Job, started.id)
        assert child is not None
        child.state = "waiting-for-operator"
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.state = "running"
        row.result = RunSwitchOperationResult(
            workload_intent_ordinal=ordinal,
            phase_index=start_index,
            item_index=0,
            phase="start",
            completed_phases=[phase.kind for phase in plan.phases[:start_index]],
            child_operation_id=started.id,
            phase_results=[
                RunSwitchStartResult(phase="start", run_id=started.owner_id)
            ],
        ).model_dump(mode="json")
    return service, operation, start_index


def test_parked_start_with_an_established_run_completes_without_an_operator(
    tmp_path: Path,
) -> None:
    """A lost start acknowledgement must not require manual recovery.

    The Controller never received the start result, but the run is up and
    published.  The lifecycle owner observes that exact run, the phase accepts
    the established effect, and the ordinary final verification confirms it.
    """

    service, operation, start_index = _parked_start_switch(tmp_path, healthy=True)

    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    view = service.get(operation.operation_id)
    assert view.state == "succeeded", view.status_reason
    completed = _result(view).completed_phases
    # The parked start was accepted exactly once, through the ordinary
    # checkpoint, so no effect is repeated by the observation.
    assert completed.count("start") == 1
    assert "final_verify" in completed
    # The start phase was accepted at its own checkpoint, not replayed earlier.
    assert start_index < len(completed)


def test_parked_start_after_observation_deadline_keeps_exact_effect_pending(
    tmp_path: Path,
) -> None:
    """A deadline slows observation but cannot declare an unknown effect failed."""

    service, operation, _start_index = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    assert service.tick() is True
    now[0] += timedelta(seconds=121)
    assert service.tick() is True

    view = service.get(operation.operation_id)
    assert view.state == "waiting-for-operator"
    assert "start-observation-expired" in (view.status_reason or "")
    assert view.result is not None and view.result.observation_due_at is not None
    assert view.result.observation_due_at == now[0] + timedelta(seconds=60)
    assert service.tick() is False
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    now[0] = view.result.observation_due_at
    for _ in range(4):
        service.tick()
    assert service.get(operation.operation_id).state == "succeeded"


def test_parked_start_still_progressing_is_observed_before_final_success(
    tmp_path: Path,
) -> None:
    """A lost start result during a real load is not a failed launch."""

    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]

    assert service.tick() is True
    held = service.get(operation.operation_id)
    assert held.state == "running"
    assert held.result is not None
    assert held.result.child_operation_id is not None
    assert held.result.observation_due_at is not None

    assert service.tick() is False
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    now[0] += timedelta(seconds=5)
    for _ in range(4):
        service.tick()
    assert service.get(operation.operation_id).state == "succeeded"


def test_restart_interrupted_start_keeps_exact_child_when_effect_is_uncertain(
    tmp_path: Path,
) -> None:
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    with service._sessions.begin() as session:
        parent = session.get(Job, operation.operation_id)
        assert parent is not None and isinstance(parent.result, dict)
        child_id = parent.result.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None
        owner_id = child.payload.get("owner_id")
        assert isinstance(owner_id, str)
        run = session.get(RecipeRun, owner_id)
        assert run is not None
        run.state = "lost"
        for node in session.scalars(select(RunNode).where(RunNode.run_id == owner_id)):
            node.state = "failed"

    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == "waiting-for-operator"
    assert view.result is not None
    assert view.result.child_operation_id == child_id


def test_restart_retry_due_is_projected_without_replacing_the_child(
    tmp_path: Path,
) -> None:
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    due = NOW + timedelta(seconds=7)
    with service._sessions.begin() as session:
        parent = session.get(Job, operation.operation_id)
        assert parent is not None and isinstance(parent.result, dict)
        child_id = parent.result.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "queued"
        agent_child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
        )
        assert agent_child is not None
        agent_child.state = "waiting-for-operator"
        agent_child.retry_disposition = "retry"
        agent_child.retry_due_at = due
        agent_child.status_reason = "exact lifecycle retry scheduled"

    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == "running"
    assert view.status_reason == "exact lifecycle retry scheduled"
    assert view.result is not None
    assert view.result.child_operation_id == child_id
    assert view.result.observation_due_at == due


def test_scoped_cleanup_is_allowed_without_launch_readiness(tmp_path: Path) -> None:
    """Removing work must not depend on being able to start work.

    The launch planner is made unavailable, so a removal plan that consulted
    launch readiness would fail here exactly as it would on a Spark whose
    admission evidence has aged out.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("launch admission is unavailable")

    lifecycle.preview_run = unavailable  # type: ignore[method-assign]
    try:
        cleanup = service.preview_cleanup(installation.owner_id, actor="admin")
    finally:
        del lifecycle.preview_run

    assert cleanup.allowed is True, [reason.code for reason in cleanup.blockers]
    assert cleanup.action == "cleanup"
    assert [phase.kind for phase in cleanup.phases] == ["uninstall", "final_verify"]
    assert not any(
        reason.code.startswith(("run-switch.run_admission", "run-switch.insufficient"))
        for reason in cleanup.blockers
    )


def test_scoped_cleanup_is_blocked_while_a_run_is_active(tmp_path: Path) -> None:
    """The uninstall assessment stays the authority for whether removal may run."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
    lifecycle.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    cleanup = service.preview_cleanup(installation.owner_id, actor="admin")

    assert cleanup.allowed is False
    assert any(
        "uninstall.active_run" in reason.detail for reason in cleanup.blockers
    ), [reason.detail for reason in cleanup.blockers]


def _make_install_specs_missing_placement_authority(
    sessions,
    installation_id: str,
    nodes: tuple[str, ...],
    *,
    resumable: bool = True,
) -> None:
    """Represent an exact old successful install with an invalid launch schema."""

    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        stored_plan = json.loads(json.dumps(installation.plan))
        install_job = session.scalar(
            select(Job).where(
                Job.kind == "recipe.install",
                Job.payload["owner_id"].as_string() == installation_id,
            )
        )
        assert install_job is not None and install_job.result is not None
        operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == install_job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert {operation.node_id for operation in operations} == set(nodes)
        for index, operation in enumerate(operations):
            payload = json.loads(json.dumps(operation.payload))
            compiled = payload["compiled_execution_plan"]
            placement = compiled["runtime"]["placement"]
            assert "memory_floor_bytes" in placement
            assert "memory_kind" in placement
            placement.pop("memory_floor_bytes")
            placement.pop("memory_kind")
            payload["compiled_execution_plan"] = compiled
            operation.payload = payload
            operation.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
            operation.current_attempt = 1
            evidence = install_job.result["node_evidence"][operation.node_id]
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW + timedelta(minutes=1),
                    agent_certificate_serial=f"serial-{index}",
                    state="succeeded",
                    result=evidence,
                )
            )
            stored_plan["compiled_execution_plans"][operation.node_id] = compiled
            agent_node = session.get(AgentNode, operation.node_id)
            assert agent_node is not None
            agent_node.capabilities = sorted(
                set(agent_node.capabilities or [])
                | {"recipe.reconcile", "recipe.reconcile.v1"}
                | ({"agent.lifecycle.resume.exact.v1"} if resumable else set())
            )
        plan_digest = installation_plan_digest_from_stored_document(stored_plan)
        stored_plan["plan_digest"] = plan_digest
        installation.plan_digest = plan_digest
        job_payload = json.loads(json.dumps(install_job.payload))
        job_payload["plan_digest"] = plan_digest
        install_job.payload = job_payload
        install_job.payload_digest = hashlib.sha256(
            canonical_message(job_payload)
        ).hexdigest()
        for operation in operations:
            payload = json.loads(json.dumps(operation.payload))
            payload["plan_digest"] = plan_digest
            operation.payload = payload
            operation.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
        installation.plan = stored_plan


def _record_successful_reconcile_member(
    sessions, lifecycle, job_id: str, node_id: str
) -> dict[str, object]:
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job_id,
                AgentOperation.node_id == node_id,
            )
        )
        assert child is not None
        payload = RecipeReconcilePayload.model_validate(child.payload)
        source_operation = session.get(AgentOperation, payload.install_operation_id)
        assert source_operation is not None
        source_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == source_operation.id,
                AgentOperationAttempt.attempt == source_operation.current_attempt,
            )
        )
        assert source_attempt is not None
        receipt = RecipeReconcileResult(
            reconciled=True,
            node_id=payload.node_id,
            installation_id=payload.installation_id,
            install_operation_id=payload.install_operation_id,
            install_operation_payload_sha256=(payload.install_operation_payload_sha256),
            plan_digest=payload.plan_digest,
            recipe_revision_id=payload.recipe_revision_id,
            recipe_content_sha256=payload.recipe_content_sha256,
            compiled_spec_canonical_sha256=(payload.compiled_spec_canonical_sha256),
            removed_bytes=1,
            cleanup_receipt_sha256=hashlib.sha256(
                f"cleanup:{node_id}".encode()
            ).hexdigest(),
        )
        evidence = receipt.model_dump(mode="json")
    with sessions.begin() as session:
        child = session.get(AgentOperation, child.id)
        assert child is not None
        child.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=child.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=NOW + timedelta(minutes=1),
                agent_certificate_serial=source_attempt.agent_certificate_serial,
                state="succeeded",
                result=evidence,
            )
        )
    lifecycle.record_node_result(job_id, node_id, succeeded=True, evidence=evidence)
    return evidence


def test_reconcile_review_binds_opaque_invalid_launch_spec_and_keeps_uninstall_strict(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, nodes, resumable=False
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    upgrade_required = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert not upgrade_required.allowed
    assert any(
        reason.code == "run-switch.reconcile.agent_upgrade_required"
        and "agent.lifecycle.resume.exact.v1" in reason.detail
        for reason in upgrade_required.blockers
    ), [(reason.code, reason.detail) for reason in upgrade_required.blockers]
    with sessions.begin() as session:
        for node_id in nodes:
            agent_node = session.get(AgentNode, node_id)
            assert agent_node is not None
            agent_node.capabilities = sorted(
                set(agent_node.capabilities or []) | {"agent.lifecycle.resume.exact.v1"}
            )

    plan = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )

    assert plan.allowed, [(reason.code, reason.detail) for reason in plan.blockers]
    assert plan.cleanup_mode == "reconcile"
    assert plan.reconciliation_authority is not None
    assert [target.node_id for target in plan.reconciliation_authority.targets] == list(
        nodes
    )
    assert all(
        target.state == "pending" and target.cleanup_receipt_sha256 is None
        for target in plan.reconciliation_authority.targets
    )
    with pytest.raises(
        RecipeOperationConflict, match="stored installation plan is invalid"
    ):
        lifecycle.preview_uninstall(installation.owner_id)
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.kind == "recipe.reconcile")) is None
        )


def test_reconcile_run_switch_releases_install_claims_after_exact_group_receipts(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, nodes
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    preview = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert preview.allowed, [
        (reason.code, reason.detail) for reason in preview.blockers
    ]

    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            plan_digest=preview.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    child_id = _child_operation_id(service.get(operation.operation_id))
    assert child_id is not None
    with sessions.begin() as session:
        child_job = session.get(Job, child_id)
        assert child_job is not None and child_job.kind == "recipe.reconcile"
        child_operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == child_job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert [item.node_id for item in child_operations] == sorted(nodes)
        receipts: dict[str, dict[str, object]] = {}
        for index, child in enumerate(child_operations):
            payload = RecipeReconcilePayload.model_validate(child.payload)
            receipt = RecipeReconcileResult(
                reconciled=True,
                node_id=payload.node_id,
                installation_id=payload.installation_id,
                install_operation_id=payload.install_operation_id,
                install_operation_payload_sha256=(
                    payload.install_operation_payload_sha256
                ),
                plan_digest=payload.plan_digest,
                recipe_revision_id=payload.recipe_revision_id,
                recipe_content_sha256=payload.recipe_content_sha256,
                compiled_spec_canonical_sha256=(payload.compiled_spec_canonical_sha256),
                removed_bytes=1,
                cleanup_receipt_sha256=hashlib.sha256(
                    f"cleanup:{child.node_id}".encode()
                ).hexdigest(),
            )
            evidence = receipt.model_dump(mode="json")
            receipts[child.node_id] = evidence
            source_operation = session.get(AgentOperation, payload.install_operation_id)
            assert source_operation is not None
            source_attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == source_operation.id,
                    AgentOperationAttempt.attempt == source_operation.current_attempt,
                )
            )
            assert source_attempt is not None
            child.current_attempt = 1
            session.add(
                AgentOperationAttempt(
                    operation_id=child.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW + timedelta(minutes=1),
                    agent_certificate_serial=source_attempt.agent_certificate_serial,
                    state="succeeded",
                    result=evidence,
                )
            )

    lifecycle.record_node_result(
        child_id, nodes[0], succeeded=True, evidence=receipts[nodes[0]]
    )
    with sessions() as session:
        held = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert held and all(item.state == "active" for item in held)

    lifecycle.record_node_result(
        child_id, nodes[1], succeeded=True, evidence=receipts[nodes[1]]
    )
    with sessions() as session:
        row = session.get(RecipeInstallation, installation.owner_id)
        assert row is not None and row.state == "uninstalled"
        released = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert released and all(item.state == "released" for item in released)

    for _ in range(6):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()
    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    cleanup_result = next(
        item
        for item in _result(completed).phase_results
        if isinstance(item, RunSwitchCleanupVerifyResult)
    )
    assert cleanup_result.cleanup_mode == "reconcile"
    assert cleanup_result.exact_reconciliation_receipts is True
    assert {item.node_id for item in cleanup_result.reconciliation_receipts} == set(
        nodes
    )


def test_new_reconcile_review_reuses_exact_partial_receipt_and_releases_last_claim(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, nodes
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    first_plan = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert first_plan.allowed, [
        (item.code, item.detail) for item in first_plan.blockers
    ]
    first = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            plan_digest=first_plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    first_child_id = _child_operation_id(service.get(first.operation_id))
    assert first_child_id is not None

    first_receipt = _record_successful_reconcile_member(
        sessions, lifecycle, first_child_id, nodes[0]
    )
    lifecycle.record_node_result(
        first_child_id,
        nodes[1],
        succeeded=False,
        evidence={
            "failure_kind": "temporary-dependency",
            "reason": "temporary dependency",
        },
    )
    for _ in range(6):
        if service.get(first.operation_id).state not in {"queued", "running"}:
            break
        service.tick()
    assert service.get(first.operation_id).state == "failed"
    with sessions() as session:
        partial = session.get(RecipeInstallation, installation.owner_id)
        assert partial is not None and partial.state == "partial"
        members = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.owner_id)
                .order_by(InstallationNode.rank)
            )
        )
        assert [(node.node_id, node.state) for node in members] == [
            (nodes[0], "uninstalled"),
            (nodes[1], "failed"),
        ]
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert claims and all(item.state == "active" for item in claims)

    retry_plan = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert retry_plan.allowed, [
        (item.code, item.detail) for item in retry_plan.blockers
    ]
    assert retry_plan.reconciliation_authority is not None
    assert [
        (target.node_id, target.state, target.cleanup_receipt_sha256)
        for target in retry_plan.reconciliation_authority.targets
    ] == [
        (
            nodes[0],
            "reconciled",
            first_receipt["cleanup_receipt_sha256"],
        ),
        (nodes[1], "pending", None),
    ]
    retry = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            plan_digest=retry_plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    retry_child_id = _child_operation_id(service.get(retry.operation_id))
    assert retry_child_id is not None
    with sessions() as session:
        retry_children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == retry_child_id
                )
            )
        )
        assert [(child.node_id, child.kind) for child in retry_children] == [
            (nodes[1], "recipe.reconcile")
        ]
    _record_successful_reconcile_member(sessions, lifecycle, retry_child_id, nodes[1])
    with sessions() as session:
        completed = session.get(RecipeInstallation, installation.owner_id)
        assert completed is not None and completed.state == "uninstalled"
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert claims and all(item.state == "released" for item in claims)


def test_scoped_cleanup_removes_the_installation_through_run_switch(
    tmp_path: Path,
) -> None:
    """Run/Switch owns the removal, its child reference and its verification."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == "running"
    child_id = _child_operation_id(view)
    assert child_id is not None

    for node_id in nodes:
        lifecycle.record_node_result(
            child_id,
            node_id,
            succeeded=True,
            evidence={"uninstalled": True, "removed_model_bytes": 1},
        )

    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    assert "uninstall" in _result(completed).completed_phases
    with sessions() as session:
        row = session.get(RecipeInstallation, installation.owner_id)
        assert row is None or row.state == "uninstalled"


def _planned_installation(tmp_path: Path, *, nodes: int = 2):
    """Admit a plan that is persisted but never launched on any node."""

    sessions, lifecycle, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes
    )
    admission = lifecycle._install_admission
    plan = admission.plan_install(mapping_id, build_id, now=NOW)
    assert plan.allowed, [
        (node.node_id, reason.code) for node in plan.nodes for reason in node.blockers
    ]
    installation_id = admission.accept_install(plan, actor="admin", now=NOW)
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "planned"
    return sessions, lifecycle, installation_id, node_ids


def test_scoped_cleanup_abandons_a_never_installed_plan(tmp_path: Path) -> None:
    """A persisted plan that never reached a node is abandoned, not removed.

    Uninstalling it would ask every Spark to remove bytes it never received.
    The installation's own assessment owns the disposition, so cleanup queues
    no agent work and the receipt still names why the record was disposed of.
    """

    sessions, lifecycle, installation_id, _nodes = _planned_installation(tmp_path)
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )

    preview = service.preview_cleanup(installation_id, actor="admin")

    assert preview.allowed is True, [reason.code for reason in preview.blockers]
    assert preview.cleanup_disposition == "abandon"
    assert any(
        reason.code == "run-switch.uninstall.abandon-never-installed"
        for reason in preview.warnings
    ), [reason.code for reason in preview.warnings]

    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation_id, request_key=str(uuid.uuid4())
        ),
        actor="admin",
    )
    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    receipt = next(
        item
        for item in _result(completed).phase_results
        if isinstance(item, RunSwitchUninstallResult)
    )
    assert receipt.disposition == "abandoned"
    assert receipt.reason == "installation-not-installed"
    # A restarted phase replays the disposal, so the effect is idempotent.
    replay = lifecycle.abandon_never_installed(installation_id)
    assert replay == {
        "installation_id": installation_id,
        "disposition": "abandoned",
    }
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "uninstalled"
        members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        assert members
        assert all(node.state == "uninstalled" for node in members)
        # The admission reservation must not leak for an abandoned plan.
        assert all(
            reservation.state == "released"
            for reservation in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation_id
                )
            )
        )


def test_scoped_cleanup_refuses_a_planned_row_with_install_evidence(
    tmp_path: Path,
) -> None:
    """A planned row that contradicts itself stays a reported blocker.

    Installed bytes or a recorded install evidence digest on a never-launched
    plan is exactly the contradiction that must not be silently abandoned, so
    the uninstall assessment keeps refusing it under
    ``run-switch.uninstall-blocked``.
    """

    sessions, lifecycle, installation_id, _nodes = _planned_installation(tmp_path)
    with sessions.begin() as session:
        member = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_id
            )
        )
        assert member is not None
        member.state = "installing"
        member.installed_bytes = 120
        member.evidence_digest = "a" * 64
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )

    preview = service.preview_cleanup(installation_id, actor="admin")

    assert preview.allowed is False
    assert preview.cleanup_disposition == "uninstall"
    assert any(
        reason.code == "run-switch.uninstall-blocked"
        and "uninstall.installation_not_uninstallable" in reason.detail
        for reason in preview.blockers
    ), [(reason.code, reason.detail) for reason in preview.blockers]
    with pytest.raises(
        RunSwitchOperationConflict, match="run-switch.uninstall-blocked"
    ):
        service.apply_cleanup(
            RunSwitchCleanupApplyRequest(
                installation_id=installation_id, request_key=str(uuid.uuid4())
            ),
            actor="admin",
        )
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "planned"


def test_parked_switch_observes_recovered_child_after_controller_restart(
    tmp_path: Path,
) -> None:
    """A parked parent must not miss its child's later exact success."""
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    with service._sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.state = "waiting-for-operator"
    restarted = RunSwitchOperationService(
        service._sessions,
        lifecycle=service._lifecycle,
        clock=lambda: now[0],
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    for _ in range(4):
        restarted.tick()
    recovered = restarted.get(operation.operation_id)
    assert recovered.state == "succeeded", recovered.status_reason
    assert _result(recovered).completed_phases.count("start") == 1


@pytest.mark.parametrize("ending", ["recovered", "cancelled", "superseded", "revoked"])
def test_temporary_phase_failure_preserves_exact_intent_across_restart(
    tmp_path: Path, ending: str
) -> None:
    class InterruptedTransfer(RecordingArtifactExecutor):
        def __init__(self):
            super().__init__()
            self.unavailable = True
            self.identities = []

        def execute(self, plan, phase, **kwargs):
            if phase.kind == "transfer":
                self.identities.append((plan.plan_digest, kwargs["request_key"]))
                if self.unavailable:
                    raise httpx.ConnectError("NAS temporarily disconnected")
            return super().execute(plan, phase, **kwargs)

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = InterruptedTransfer()
    now = [NOW]

    def restarted():
        result = _service(
            sessions,
            now[0],
            lifecycle,
            executor,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        result._clock = lambda: now[0]
        return result

    service = restarted()
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        original = dict(row.payload)
    for _ in range(7):
        assert service.tick()
        waiting = service.get(operation.operation_id)
        assert waiting.state == "running", waiting.status_reason
        assert (
            waiting.result is not None and waiting.result.observation_due_at is not None
        )
        due = waiting.result.observation_due_at
        assert now[0] < due <= now[0] + timedelta(seconds=60)
        service = restarted()
        assert service.tick() is False
        now[0] = due
    if ending == "cancelled":
        service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=str(uuid.uuid4()),
            reason="Stop recovery",
        )
    elif ending in {"superseded", "revoked"}:
        with sessions.begin() as session:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            if ending == "superseded":
                node.workload_intent_ordinal += 1
            else:
                node.revoked_at = now[0]
    executor.unavailable = False
    service = restarted()
    service.tick()
    current = service.get(operation.operation_id)
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.payload == original
    if ending == "recovered":
        assert current.state == "running"
        assert current.result is not None
        assert "transfer" in current.result.completed_phases
        assert len(executor.identities) == 8
    else:
        assert current.state == ("failed" if ending == "revoked" else "cancelled")
        assert len(executor.identities) == 7
    assert len(set(executor.identities)) == 1


@pytest.mark.parametrize("kind", ["artifact-job-cancellation", "recipe.stop"])
def test_late_dependency_receipt_resumes_original_checkpoint_after_deadline(
    tmp_path: Path, kind: str
) -> None:
    class PendingDependency(RecordingArtifactExecutor):
        pending = True
        calls_while_pending = 0

        def execute(self, plan, phase, **kwargs):
            if self.pending:
                self.calls_while_pending += 1
                raise RunSwitchIssuedWorkloadPending(
                    kind=kind,
                    owner_id="exact-old-owner",
                    job_id=dependency,
                    observe_due_at=NOW,
                    observation_deadline=NOW + timedelta(seconds=120),
                )
            return super().execute(plan, phase, **kwargs)

    dependency = str(uuid.uuid4())
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = PendingDependency()
    now = [NOW + timedelta(seconds=121)]
    service = _service(
        sessions,
        now[0],
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert service.tick()
    held = service.get(operation.operation_id)
    assert held.state == "running", held.status_reason
    assert held.result is not None
    assert held.result.observation_due_at is not None
    assert held.result.observation_due_at == now[0] + timedelta(seconds=60)
    assert held.status_reason is not None and dependency in held.status_reason
    assert service.tick() is False
    executor.pending = False
    now[0] = held.result.observation_due_at
    restarted = _service(
        sessions,
        now[0],
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    assert restarted.tick()
    recovered = restarted.get(operation.operation_id)
    assert recovered.state == "running"
    assert (
        recovered.result is not None and "transfer" in recovered.result.completed_phases
    )
    assert executor.calls_while_pending == 1


def test_shared_admission_contention_preserves_operation_for_retry(
    tmp_path: Path,
) -> None:
    """A busy child enqueue must retain intent instead of escaping the worker."""
    from vonk_control.admission_locking import AdmissionLockBusy

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )

    class BusyExecutor(RecordingArtifactExecutor):
        def execute(self, *args, **kwargs):
            raise AdmissionLockBusy("admission lock node is busy")

    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=BusyExecutor(),
    )
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    for _ in range(12):
        service.tick()
        with sessions() as session:
            job = session.get(Job, operation.operation_id)
            assert job is not None
            if job.status_reason and "capacity writer" in job.status_reason:
                assert job.state == "running"
                assert "retry at" in job.status_reason
                break
    else:
        pytest.fail("shared admission contention did not schedule a durable retry")
