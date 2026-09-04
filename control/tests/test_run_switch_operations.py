from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vonk_control.auth import CursorCodec
from vonk_control.models import (
    ClusterMapping,
    ClusterMappingNode,
    Job,
    LocalRecipeRevision,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.run_switch_contract import (
    InvocationMetadata,
    RunSwitchApplyRequest,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    PhaseExecution,
    RunSwitchOperationProvider,
    RunSwitchOperationService,
)

from .test_recipe_operations import installed_recipe, setup_services

MODEL_ARTIFACT = "c" * 64
MODEL_ARTIFACT_SET = "f" * 64


class CompleteArtifactInspector:
    def __init__(self, *, reclaimable_bytes: int = 0, missing_spark_bytes: int = 0) -> None:
        self.reclaimable_bytes = reclaimable_bytes
        self.missing_spark_bytes = missing_spark_bytes

    def inspect(
        self,
        _session,
        *,
        model_version_sha256,
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


class RecordingArtifactExecutor:
    def __init__(self, *, child_transfer: bool = False, bad_verify: bool = False) -> None:
        self.child_transfer = child_transfer
        self.bad_verify = bad_verify
        self.calls: list[str] = []
        self.children: dict[str, SimpleNamespace] = {}

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
        self.calls.append(phase.kind)
        if phase.kind == "transfer" and self.child_transfer:
            child_id = str(uuid.uuid4())
            self.children[child_id] = SimpleNamespace(state="queued", result=None)
            return PhaseExecution(
                operation_id=child_id,
                result={"phase": phase.kind, "checkpoint": item_index},
            )
        if phase.kind == "verify":
            digests = ["d" * 64] if self.bad_verify else [MODEL_ARTIFACT]
            return PhaseExecution(
                result={
                    "verified": True,
                    "verified_digests": digests,
                    "verified_image_digest": (
                        "sha256:" + "1" * 64
                    ),
                    "verified_oci_layout_sha256": "3" * 64,
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
        return PhaseExecution(result={"copied_bytes": 0})

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


def _request(sessions, node_id: str, *, action: str = "run", retention: str = "retain-cached"):
    with sessions() as session:
        revision = session.scalar(
            select(LocalRecipeRevision).where(LocalRecipeRevision.lifecycle == "resolved")
        )
        assert revision is not None
        model = revision.document["model"]
        model_digest = model["content_sha256"]
    return RunSwitchPreviewRequest(
        model_version_sha256=model_digest,
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
        "transfer",
        "verify",
        "prepare",
        "start",
        "final_verify",
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
    progressed = service.get(operation.operation_id)
    assert progressed.current_phase == "prepare"
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
        "start",
        "final_verify",
    ]


def test_artifact_child_checkpoint_and_digest_mismatch_fail_closed(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    artifact_executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
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
    child_id = pending.result["child_operation_id"]
    artifact_executor.children[child_id].state = "succeeded"
    artifact_executor.children[child_id].result = {"copied_bytes": 0}
    assert service.tick() is True
    assert service.get(operation.operation_id).current_phase == "verify"

    bad_artifacts = RecordingArtifactExecutor(bad_verify=True)
    bad_service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        bad_artifacts,
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
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
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
    child_id = service.get(operation.operation_id).result["child_operation_id"]
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
    artifact_executor.children[child_id].result = {"copied_bytes": 1024}
    assert restarted.tick() is True
    completed_transfer = restarted.get(operation.operation_id)
    assert completed_transfer.progress.completed_bytes == 1024
    assert completed_transfer.progress.members[0].completed_bytes == 1024
    assert completed_transfer.progress.members[0].state == "succeeded"


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
    assert item["supported_actions"] == []
    assert item["progress"]["total_bytes_known"] is True
    assert item["progress"]["members"][0]["member_id"] == nodes[0]
    assert "phase_index" not in item["progress"]
    assert item["progress"]["checkpoint"]["digest"] == operation.plan_digest
    assert datetime.fromisoformat(item["created_at"]).tzinfo == UTC
    assert provider.get_operation(operation.operation_id)["id"] == operation.operation_id


def test_activity_provider_integrates_with_global_cursor_and_detail_projection(
    tmp_path: Path,
) -> None:
    try:
        from vonk_control.operation_api import (
            OperationProvider,
            get_operation_from_providers,
            merge_operation_providers,
            operation_detail_response,
        )
    except ImportError:
        pytest.skip("global Activity provider seam is supplied by the integration branch")

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
