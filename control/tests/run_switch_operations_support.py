from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    canonical_message,
)
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distribution_executor.receipts import _ChildView
from vonk_control.install_admission import (
    InstallAdmissionBusy,
)
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.model_cache import (
    ArtifactSetManifest,
    ArtifactSpec,
    ModelCacheService,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    Job,
)
from vonk_control.recipe_operations import (
    RecipeOperationService,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchDistributionChildResult,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPreviewRequest,
    RunSwitchStartResult,
    RunSwitchTargetTransferEvidenceResult,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    PhaseExecution,
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationService,
    _phase_result,
)
from vonk_control.runtime.adapters import resolve_runtime_adapter

from .recipe_operations_support import (
    NOW,
    installed_recipe,
    setup_services,
)
from .runtime_identity_support import (
    set_inventory_fingerprint,
)
from .test_lifecycle_preflight import _finish

_FIXTURE_ADAPTER = resolve_runtime_adapter("vllm", {"node_count": 1})


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


def _target_copy_evidence(
    plan, phase, progress=None
) -> RunSwitchTargetTransferEvidenceResult:
    del progress
    node_id = phase.node_ids[0] if getattr(phase, "node_ids", ()) else "spk_" + "0" * 32
    return RunSwitchTargetTransferEvidenceResult(
        phase="transfer",
        subphase="target-copy",
        node_id=node_id,
        copied_bytes=getattr(plan.storage, "missing_spark_bytes", 0),
    )


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
        "distribution_publisher": "test",
        "distribution_slug": "recipe",
        "distribution_content_sha256": "a" * 64,
        "image_digest": image,
        "oci_archive_sha256": layout,
        "image_bytes": size,
        "local_image_config_id": "sha256:" + "4" * 64,
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
    def __init__(self, *, missing_spark_bytes: int = 0) -> None:
        self.missing_spark_bytes = missing_spark_bytes

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        now: datetime,
    ) -> ArtifactInspection:
        del session, model_content_sha256, recipe_revision_id, now
        required = 1024 * len(node_ids)
        missing_spark_bytes = self.missing_spark_bytes
        return ArtifactInspection(
            required_bytes=required,
            reused_bytes=required - missing_spark_bytes,
            copied_bytes=missing_spark_bytes,
            missing_nas_bytes=0,
            missing_spark_bytes=missing_spark_bytes,
            artifact_count=1,
            nas_coverage="complete",
            spark_coverage="complete" if missing_spark_bytes == 0 else "partial",
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


def _fixture_execution(plan, phase, *, result=None, **kwargs) -> PhaseExecution:
    """Publish canonical evidence from a synthetic successful phase producer."""
    if isinstance(result, dict):
        result = dict(result)
        if result == {"phase": phase.kind}:
            if phase.kind == "prepare":
                result = {"prepared": True}
            elif phase.kind in {"start", "stop"}:
                result = {"run_id": plan.run_id or str(uuid.uuid4())}
            elif phase.kind == "final_verify":
                result = {
                    "final_verified": True,
                    "run_id": plan.run_id or str(uuid.uuid4()),
                    "state": "running",
                    "route_state": "published",
                    "healthy": True,
                    "ranks": [],
                }
        result = _phase_result(result, phase=phase)
    return PhaseExecution(result=result, **kwargs)


def _fixture_child_result(
    plan, phase, payload=None
) -> RunSwitchDistributionChildResult:
    """Typed measured child checkpoint, independent of the terminal receipt."""
    payload = payload or {}
    raw_progress = dict(payload.get("progress", {}))
    if "operation" in payload:
        raw_progress["operation"] = payload["operation"]
    total = raw_progress.get("total_bytes")
    raw_progress["total_bytes_known"] = total is not None
    raw_members = (
        raw_progress.get("members")
        or payload.get("members")
        or [{"node_id": phase.node_ids[0], "phase": "transfer", "state": "pending"}]
    )
    raw_progress["members"] = raw_members
    evidence = payload.get("evidence", [])
    return RunSwitchDistributionChildResult.model_validate_json(
        canonical_message(
            {
                "phase": "transfer",
                "subphase": "target-copy",
                "progress": raw_progress,
                "members": raw_members,
                "evidence": evidence,
                "reason": payload.get("reason"),
                "failure_kind": payload.get("failure_kind"),
                "error_code": payload.get("error_code"),
            }
        ),
        strict=True,
    )


def _replace_child(executor, child_id, *, state=None, payload=None, result=None):
    child = executor.children[child_id]
    if payload is not None:
        prior = child.result
        assert isinstance(prior, RunSwitchDistributionChildResult)
        node_id = prior.members[0].node_id
        phase = RunSwitchPhase(
            index=0,
            kind="transfer",
            subphase="target-copy",
            state="planned",
            node_ids=[node_id],
            detail="Fixture child checkpoint",
        )
        result = _fixture_child_result(None, phase, payload)
    updated = replace(child, state=state or child.state, result=result or child.result)
    executor.children[child_id] = updated
    return updated


class RecordingArtifactExecutor:
    def __init__(self, *, child_transfer: bool = False) -> None:
        self.child_transfer = child_transfer
        self.calls: list[str] = []
        self.children: dict[str, _ChildView] = {}
        self.abandoned: list[str] = []

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
            self.children[child_id] = _ChildView(
                state="queued", result=_fixture_child_result(plan, phase)
            )
            return PhaseExecution(
                operation_id=child_id,
                result=None,
            )
        if phase.kind == "verify":
            runtime_image = getattr(
                getattr(plan, "preparation", None), "runtime_image", None
            )
            image_digest = getattr(runtime_image, "image_digest", "sha256:" + "1" * 64)
            archive_sha256 = getattr(runtime_image, "oci_layout_sha256", "3" * 64)
            return _fixture_execution(
                plan,
                phase,
                result={
                    "verified": True,
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": image_digest,
                    "verified_oci_layout_sha256": archive_sha256,
                },
            )
        if phase.subphase == "runtime-image":
            # A Spark no installation holds the image on gets it copied; the
            # Controller's stored image is ready for that copy.
            return ColdStartPhaseExecutor().execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        return _fixture_execution(
            plan, phase, result=_target_copy_evidence(plan, phase)
        )

    def get(self, operation_id: str) -> _ChildView:
        return self.children[operation_id]

    def abandon(self, _session, operation_id: str, _now, *, reason: str) -> bool:
        """Mirror the durable executor: only a parked child is closed."""

        child = self.children.get(operation_id)
        if child is None or child.state != "needs-operator":
            return False
        self.children[operation_id] = replace(
            child,
            state="cancelled",
            result=child.result.model_copy(update={"reason": reason}),
        )
        self.abandoned.append(operation_id)
        return True


class SynchronousPhaseExecutor:
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
        return _fixture_execution(plan, phase, result={"phase": phase.kind})


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
            return _fixture_execution(
                plan,
                phase,
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
                },
            )
        if phase.subphase == "runtime-image":
            return _fixture_execution(
                plan,
                phase,
                result={
                    "runtime_image": _runtime_receipt(plan),
                    "image_digest": plan.build.image_digest,
                    "oci_layout_sha256": plan.build.oci_layout_sha256,
                    "image_bytes": plan.build.image_bytes,
                },
            )
        if phase.kind == "transfer" and phase.subphase == "target-copy":
            return _fixture_execution(
                plan, phase, result=_target_copy_evidence(plan, phase)
            )
        if phase.kind == "verify":
            return _fixture_execution(
                plan,
                phase,
                result={
                    "verified": True,
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": plan.image_digest,
                    "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                },
            )
        if phase.subphase == "runtime-plan":
            mapping = getattr(plan, "mapping", None)
            mapping_id = getattr(mapping, "mapping_id", None) or str(uuid.uuid4())
            installation_id = getattr(plan, "installation_id", None) or str(
                uuid.uuid4()
            )
            return _fixture_execution(
                plan,
                phase,
                result={
                    "installation_id": installation_id,
                    "mapping_id": mapping_id,
                    "install_plan_digest": plan.plan_digest,
                    "compiled_plan_persisted": True,
                },
            )
        if phase.subphase == "runtime-install":
            return _fixture_execution(
                plan,
                phase,
                result={
                    "installation_id": getattr(plan, "installation_id", None)
                    or str(uuid.uuid4())
                },
            )
        # ``runtime-install`` represents the real admission/compile boundary
        # in this focused driver; the asserted event ordering is the contract.
        return _fixture_execution(plan, phase, result={"prepared": True})

    def get(self, operation_id: str):
        raise KeyError(operation_id)


def _request(
    sessions,
    node_id: str,
    *,
    action: Literal["run", "switch"] = "run",
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

    def __init__(
        self, lifecycle, sessions, clock, runtime_install_busy_attempts=0
    ) -> None:
        self.events: list[str] = []
        self.runtime_install_busy_attempts = runtime_install_busy_attempts
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
            if (
                phase.subphase == "runtime-install"
                and self.runtime_install_busy_attempts
            ):
                self.runtime_install_busy_attempts -= 1
                raise InstallAdmissionBusy("install.capacity_busy")
            return self.delegate.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        if phase.subphase == "runtime-image":
            return _fixture_execution(
                plan,
                phase,
                result={
                    "runtime_image": _runtime_receipt(plan),
                    "image_digest": plan.build.image_digest,
                    "oci_layout_sha256": plan.build.oci_layout_sha256,
                    "image_bytes": plan.build.image_bytes,
                },
            )
        if phase.kind == "transfer":
            return _fixture_execution(
                plan, phase, result=_target_copy_evidence(plan, phase)
            )
        if phase.kind == "verify":
            return _fixture_execution(
                plan,
                phase,
                result={
                    "verified": True,
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": plan.image_digest,
                    "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                },
            )
        return _fixture_execution(plan, phase, result={"phase": phase.kind})

    def get(self, operation_id: str):
        return self.delegate.get(operation_id)


def _cold_compile_switch(
    tmp_path: Path,
    *,
    seconds: int = 716,
    slow_compiles: int = 1,
    runtime_install_busy_attempts: int = 0,
):
    """Start a real Run/Switch whose cold compile takes ``seconds`` of clock time.

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
                NodeInventoryReport.model_validate_json(
                    canonical_message(
                        {
                            "node_id": node_id,
                            "observed_at": clock.now,
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
                        }
                    )
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
    executor = _ColdCompileExecutor(
        lifecycle,
        sessions,
        clock,
        runtime_install_busy_attempts=runtime_install_busy_attempts,
    )
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

    def drive(operation_id: str | None = None) -> None:
        """Advance one due phase, answering any ordinary preflight probe."""
        operation_id = operation_id or operation.operation_id
        before = service.get(operation_id)
        due = before.next_attempt_at if before.result is not None else None
        if due is not None and due > clock.now:
            clock.now = due
        service.tick()
        view = service.get(operation_id)
        assert view.result is not None
        checkpoint = view.result.preflight
        if checkpoint is not None and next(
            iter(checkpoint.pending_jobs.values()), None
        ):
            with sessions() as session:
                pending = session.get(
                    Job, next(iter(checkpoint.pending_jobs.values()), None)
                )
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


def _host_changes_during_compile(switch) -> None:
    """Every slow compile sees the Spark host change, as a reboot or upgrade does.

    The receipt the phase was admitted on then no longer matches the host, so
    install admission refuses the identical plan until the ordinary probe runs.
    """

    changes = iter(range(1, 1_000))

    def change() -> None:
        fingerprint = f"{next(changes):064x}"
        with switch.sessions.begin() as session:
            for node_id in switch.nodes:
                set_inventory_fingerprint(session.get(AgentNode, node_id), fingerprint)

    switch.hooks.append(change)


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
        memory_floor_bytes=0,
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
        child.state = "needs-operator"
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
