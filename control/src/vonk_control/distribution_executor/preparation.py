"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, cast

from sqlalchemy import select
from vonk_agent_protocol import (
    LifecycleState,
    OperationProgress,
    ProgressPhase,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileEffectState

from .. import model_cache_states
from ..content_identity import same_image
from ..logging import redact_text
from ..model_cache import CacheOperationView, ModelCacheNotFound
from ..model_cache_contract import (
    ModelCacheDownloadPreviewResponse,
    ModelCacheDownloadResult,
)
from ..models import (
    CatalogDocumentRevision,
    RecipeBuild,
)
from ..run_switch_contract import (
    RunSwitchChildProgress,
    RunSwitchModelDownloadPendingResult,
    RunSwitchModelDownloadResult,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchRuntimeImageResult,
)
from ..run_switch_observation_contract import RuntimeImageBuildInput
from ..run_switch_operations import PhaseExecution
from ..run_switch_operations.publish import _persist_run_switch_runtime_image_reference
from ..runtime_image_preparation import (
    RuntimeImageReceipt,
)
from ..strict_json import read_stored_model
from ..worker_memory_contract import WorkerMemoryComponent

_LOGGER = logging.getLogger(__name__)


from .durable import DurableDistributionPhaseExecutor
from .receipts import _ChildView, _phase_receipt


class CompositeDistributionPhaseExecutor(DurableDistributionPhaseExecutor):
    """Run the Controller cache child before Spark target distribution."""

    def __init__(
        self,
        *args: Any,
        model_cache: object,
        runtime_image_preparer: Callable[..., object] | None = None,
        async_runtime_image_preparation: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._model_cache = model_cache
        self._runtime_image_preparer = runtime_image_preparer
        self._async_runtime_image_preparation = async_runtime_image_preparation
        self._runtime_image_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-image-preparation"
        )
        self._runtime_image_futures: dict[
            tuple[str, int, int], tuple[Future[RunSwitchRuntimeImageResult | None], str]
        ] = {}

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        return {
            WorkerMemoryComponent.RUNTIME_IMAGE_FUTURES: len(
                self._runtime_image_futures
            )
        }

    def close(self) -> None:
        """Leave image preparation checkpoints resumable during shutdown."""

        self._runtime_image_pool.shutdown(wait=False, cancel_futures=True)

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        if (
            phase.kind == ProfileChildPhase.PREPARE.value
            and phase.subphase == "runtime-image"
        ):
            # This is the only mutating image boundary.  It runs as its own
            # durable high-level phase so install admission cannot compile a
            # schema-2 payload until the Controller archive and receipt are
            # present.  Target-copy only consumes the persisted evidence.
            if self._async_runtime_image_preparation:
                key = (request_key, phase.index, item_index)
                submitted = self._runtime_image_futures.get(key)
                if submitted is None:
                    self._runtime_image_futures[key] = (
                        self._runtime_image_pool.submit(
                            self._prepare_runtime_image,
                            plan,
                            phase,
                            item_index=item_index,
                            actor=actor,
                            request_key=request_key,
                            progress=progress,
                        ),
                        self._clock().isoformat(),
                    )
                    return PhaseExecution(
                        waiting=True,
                        status_reason=(
                            "Runtime image preparation is running in the background; "
                            "the durable checkpoint will be resumed after a restart."
                        ),
                    )
                future, started_at = submitted
                if not future.done():
                    # Bounded by the transport's subprocess timeout; the start
                    # time makes a slow or stuck preparation visible.
                    return PhaseExecution(
                        waiting=True,
                        status_reason=(
                            "Runtime image preparation is still running in the "
                            f"background (started {started_at})."
                        ),
                    )
                del self._runtime_image_futures[key]
                error = future.exception()
                if error is not None:
                    # The tick decides retry or failure; record every cause here
                    # so no background failure is ever silent.
                    _LOGGER.warning(
                        "runtime image preparation for run/switch phase %s failed: %s: %s",
                        request_key,
                        getattr(error, "code", type(error).__name__),
                        redact_text(getattr(error, "detail", error)),
                    )
                runtime_result = future.result()
            else:
                runtime_result = self._prepare_runtime_image(
                    plan,
                    phase,
                    item_index=item_index,
                    actor=actor,
                    request_key=request_key,
                    progress=progress,
                )
            if runtime_result is None:
                raise RuntimeError("runtime image preparation returned no evidence")
            return PhaseExecution(result=_phase_receipt(runtime_result, phase=phase))
        if phase.subphase != ProfileChildPhase.MODEL_DOWNLOAD.value:
            return super().execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        if phase.kind != ProfileChildPhase.TRANSFER.value or item_index != 0:
            raise RuntimeError("invalid model-download phase")
        preparation = plan.preparation
        model = preparation.model if preparation is not None else None
        artifact_set_sha256 = (
            model.artifact_set_sha256
            if model is not None
            else plan.storage.artifact_set_sha256
        )
        artifact_count = (
            model.artifact_count
            if model is not None
            else len(plan.storage.artifact_digests)
        )
        artifact_set_bytes = (
            model.artifact_set_bytes
            if model is not None
            else plan.storage.artifact_set_bytes
        )
        if not artifact_set_sha256 or not artifact_count or not artifact_set_bytes:
            raise RuntimeError("exact model preparation is unavailable")
        preview_method = getattr(self._model_cache, "download_preview", None)
        start_method = getattr(self._model_cache, "start_download", None)
        if not isinstance(preview_method, Callable) or not isinstance(
            start_method, Callable
        ):
            raise TypeError("model-cache download provider is unavailable")
        # Content pins resolve the cache authority's current decision. Recipe
        # provenance is not a second availability gate for an identical set.
        cache_request_key = str(
            uuid.uuid5(
                uuid.UUID(request_key),
                f"model-download:{phase.index}:{artifact_set_sha256}",
            )
        )
        lookup = getattr(self._model_cache, "get_operator_request", None)
        if callable(lookup):
            try:
                existing, _action, _selector = cast(
                    tuple[CacheOperationView, object, object],
                    lookup(cache_request_key, actor=actor),
                )
            except ModelCacheNotFound:
                existing = None
            if existing is not None:
                return PhaseExecution(
                    operation_id=existing.id,
                    result=_phase_receipt(self._cache_result(existing), phase=phase),
                )
        raw_preview = preview_method(artifact_set_sha256=artifact_set_sha256)
        if not isinstance(raw_preview, Mapping):
            raise TypeError("model-cache download preview is unavailable")
        preview = read_stored_model(
            ModelCacheDownloadPreviewResponse,
            {
                key: value
                for key, value in raw_preview.items()
                if not key.startswith("_")
            },
        )
        if preview.artifact_set_sha256 != artifact_set_sha256:
            raise RuntimeError("model-cache download preview content is unavailable")
        # Admission revalidates the decision, persists the exact manifest and
        # handles its own blockers. A stale preview is re-fetched on the same
        # phase checkpoint and request key under the parent's durable deadline.
        view = start_method(
            actor=actor,
            request_key=cache_request_key,
            plan_digest=preview.plan_digest,
            artifact_set_sha256=artifact_set_sha256,
        )
        return PhaseExecution(
            operation_id=view.id,
            result=_phase_receipt(self._cache_result(view), phase=phase),
        )

    def _prepare_runtime_image(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> RunSwitchRuntimeImageResult | None:
        """Controller image preparation and exact target execution authorization.

        This callback is deliberately supplied only to the durable worker
        executor.  API preview, admission, and agent spec reads use the
        read-only receipt resolver in ``ControllerExecutionPlanService``.
        """

        if self._runtime_image_preparer is None:
            return None
        if plan.recipe_revision_id is None or not plan.spark_group.nodes:
            raise RuntimeError("runtime image preparation identity is unavailable")
        from ..execution_plan_service import _bind_runtime_artifacts
        from ..recipe_runtime_specs import (
            RecipeRuntimeSpecError,
            compile_runtime_spec,
            resolve_recipe_entities,
            split_option_choices,
        )
        from ..runtime_spec_contract import RuntimeSpec

        with self._sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == plan.recipe_revision_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
            if revision is None:
                raise RuntimeError(
                    "runtime image preparation recipe revision is unavailable"
                )
            build = (
                session.get(RecipeBuild, plan.recipe_build_id)
                if plan.recipe_build_id is not None
                else None
            )
            if (
                build is None
                or build.state != LifecycleState.SUCCEEDED.value
                or build.image_digest is None
            ):
                raise RuntimeError(
                    "runtime image preparation build receipt is unavailable"
                )
            package_handle = RuntimeImageBuildInput(
                image_digest=build.image_digest,
                image_reference=f"localhost/vonk/recipe-build@{build.image_digest}",
                build_input_sha256=build.build_input_sha256,
                platform="linux/arm64",
            )
            entities = resolve_recipe_entities(session, revision.document)
            option_choices, settings = split_option_choices(
                plan.mapping.parameters if plan.mapping is not None else None
            )
            try:
                resolved_models = entities.models
            except (TypeError, ValueError) as error:
                raise RecipeRuntimeSpecError(
                    "canonical model projection is invalid"
                ) from error
            runtime_specs: dict[str, RuntimeSpec] = {}
            for node in sorted(
                plan.spark_group.nodes, key=lambda item: (item.rank, item.node_id)
            ):
                runtime_spec = compile_runtime_spec(
                    entities.recipe,
                    recipe_digest=entities.recipe_digest,
                    models=resolved_models,
                    package_handle=package_handle,
                    parameters=settings,
                    option_choices=option_choices,
                    role=node.role,
                    rank=node.rank,
                )
                runtime_spec = _bind_runtime_artifacts(runtime_spec, resolved_models)
                execution_key = runtime_spec.identity.execution_sha256
                if execution_key is None:
                    raise TypeError(
                        "runtime image preparation execution identity is unavailable"
                    )
                runtime_specs[execution_key] = runtime_spec
        # Each role/rank has its own execution identity. Persist all of them
        # before install admission compiles the group. The preparer reuses the
        # immutable archive; close the read session before its receipt writes.
        execution_keys = tuple(sorted(runtime_specs))

        def before_publish(receipt: RuntimeImageReceipt) -> None:
            # Busy reference ownership yields this attempt immediately. The
            # parent releases its transaction and retries under its deadline.
            persist_reference(receipt)

        def persist_reference(receipt: RuntimeImageReceipt) -> None:
            _persist_run_switch_runtime_image_reference(
                self._sessions,
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
                execution_keys=execution_keys,
                receipt=receipt,
                clock=self._clock,
            )

        prepared: RuntimeImageReceipt | None = None
        for runtime_spec in runtime_specs.values():
            receipt = self._runtime_image_preparer(
                revision.document,
                runtime_spec,
                build,
                before_publish=before_publish,
            )
            current = (
                receipt
                if isinstance(receipt, RuntimeImageReceipt)
                else read_stored_model(RuntimeImageReceipt, receipt, strict=True)
            )
            if prepared is not None and not same_image(current, prepared):
                raise RuntimeError(
                    "runtime image preparation returned different images for the group"
                )
            prepared = current
        if prepared is None:
            raise RuntimeError("runtime image preparation returned no evidence")
        return RunSwitchRuntimeImageResult(
            phase=ProfileChildPhase.PREPARE.value,
            subphase="runtime-image",
            runtime_image=prepared,
            effective_execution_key=next(iter(runtime_specs)),
            image_digest=prepared.image_digest,
            oci_layout_sha256=prepared.oci_archive_sha256,
            image_bytes=prepared.image_bytes,
            build_id=prepared.build_id,
        )

    def get(self, operation_id: str) -> _ChildView:
        getter = getattr(self._model_cache, "get_operation", None)
        if isinstance(getter, Callable):
            try:
                view = getter(operation_id)
                return _ChildView(
                    state=self._cache_state(view.state),
                    result=_phase_receipt(self._cache_result(view)),
                )
            except ModelCacheNotFound:
                pass
        return super().get(operation_id)

    @staticmethod
    def _cache_state(state: object) -> str:
        if model_cache_states.operation_is_backoff(
            state if isinstance(state, str) else None
        ):
            return LifecycleState.RUNNING.value
        if state in {
            LifecycleState.QUEUED.value,
            LifecycleState.RUNNING.value,
            LifecycleState.SUCCEEDED.value,
            LifecycleState.FAILED.value,
            LifecycleState.CANCELLED.value,
        }:
            return str(state)
        return ProfileEffectState.UNKNOWN.value

    @staticmethod
    def _cache_result(
        view: CacheOperationView,
    ) -> RunSwitchModelDownloadResult | RunSwitchModelDownloadPendingResult:
        from ..model_cache_contract import ModelCacheOperationProgress
        from ..model_cache_progress import project_cache_progress

        if view.artifact_set_sha256 is None:
            raise RuntimeError(
                "model-cache operation content observation is unavailable"
            )
        cache = read_stored_model(ModelCacheOperationProgress, view.progress)
        progress = RunSwitchChildProgress(
            phase=ProgressPhase.MODEL_DOWNLOAD.value,
            completed_bytes=cache.downloaded_bytes,
            total_bytes=cache.expected_bytes,
            total_bytes_known=cache.expected_bytes is not None,
            operation=read_stored_model(
                OperationProgress, project_cache_progress(view.progress), from_json=True
            ),
        )
        if view.result is not None:
            if not isinstance(view.result, ModelCacheDownloadResult):
                raise RuntimeError("model-cache completion is not download evidence")
            if view.result.artifact_set_sha256 != view.artifact_set_sha256:
                raise RuntimeError("model-cache completion identity is not exact")
            return RunSwitchModelDownloadResult(
                schema_version=2,
                phase=ProfileChildPhase.TRANSFER.value,
                subphase=ProfileChildPhase.MODEL_DOWNLOAD.value,
                coverage=view.result.coverage,
                artifact_set_sha256=view.artifact_set_sha256,
                downloaded_bytes=cache.downloaded_bytes,
                total_bytes=cache.expected_bytes,
                progress=progress,
                reason=view.last_error,
                evidence=view.result,
            )
        return RunSwitchModelDownloadPendingResult(
            phase=ProfileChildPhase.TRANSFER.value,
            subphase=ProfileChildPhase.MODEL_DOWNLOAD.value,
            artifact_set_sha256=view.artifact_set_sha256,
            downloaded_bytes=cache.downloaded_bytes,
            total_bytes=cache.expected_bytes,
            progress=progress,
            reason=view.last_error,
        )
