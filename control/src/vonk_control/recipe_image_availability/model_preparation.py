"""Model preparation for exact recipe image availability."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from vonk_agent_protocol import (
    OperationProgress,
    ProgressPhase,
    RecipeImageCode,
    WaitReason,
    canonical_message,
)

from .. import model_cache_states
from ..job_documents import (
    AvailabilityModelChild,
)
from ..model_cache_contract import (
    CacheManifest,
    ModelCacheOperationProgress,
    ModelCacheRepairPreviewResponse,
)
from ..model_cache_progress import project_cache_progress
from ..operation_contract import (
    AvailabilityOperationFailure,
)
from ..recipe_image_availability_contract import (
    RecipeImageAvailabilityArtifact,
)
from ..recipe_image_availability_reader_contract import (
    AvailabilityDownloadPreview,
)
from .contracts import (
    ModelCacheOperationHandle,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityUnknown,
    _ModelQueueFailed,
    _progress,
    _read,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def _model_progress(
    self: RecipeImageAvailabilityService, value: object
) -> OperationProgress:
    parsed = _read(ModelCacheOperationProgress, value)
    if parsed is None:
        return _progress(ProgressPhase.WAITING)
    result = _read(
        OperationProgress,
        project_cache_progress(parsed.model_dump(mode="json"), self._clock()),
    )
    return result if result is not None else _progress(ProgressPhase.WAITING)


def _refresh_model_observation(
    self: RecipeImageAvailabilityService,
    child: AvailabilityModelChild,
    operation: ModelCacheOperationHandle,
) -> AvailabilityModelChild:
    failure = (
        _read(AvailabilityOperationFailure, operation.failure)
        if operation.failure is not None
        else None
    )
    return AvailabilityModelChild.model_validate_json(
        canonical_message(
            child.model_dump(mode="json")
            | {
                "id": operation.id,
                "state": operation.state,
                "progress": self._model_progress(operation.progress).model_dump(
                    mode="json"
                ),
                "artifact_set_sha256": operation.artifact_set_sha256,
                "plan_digest": operation.plan_digest,
                "failure": failure.model_dump(mode="json")
                if failure is not None
                else None,
            }
        )
    )


def _ensure_model_child(
    self: RecipeImageAvailabilityService,
    recipe_revision_id: str,
    *,
    actor: str,
    parent_request_key: str,
) -> AvailabilityModelChild | None:
    """Queue one exact durable ModelCache child for the complete model set."""

    if self._model_cache is None:
        return None
    child_request_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-availability-model:{recipe_revision_id}:{parent_request_key}",
        )
    )
    try:
        preview = AvailabilityDownloadPreview.read(
            self._model_cache.download_preview(recipe_revision_id=recipe_revision_id)
        )
        if preview is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.MODEL_CACHE_INVALID,
                "ModelCache returned an incomplete exact artifact plan",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        plan_digest = preview.plan_digest
        artifact_set_sha256 = preview.artifact_set_sha256
        new_bytes = preview.new_bytes
        manifest = self._model_cache.resolve_artifact_set(
            recipe_revision_id=recipe_revision_id
        )
        manifest_document = _read(CacheManifest, manifest.document())
        if manifest_document is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.MODEL_CACHE_INVALID,
                "ModelCache returned an incomplete artifact manifest",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if manifest.digest != artifact_set_sha256:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.MODEL_CACHE_INVALID,
                "resolved model artifact identity changed during planning",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        operation = None
        list_operations = getattr(self._model_cache, "list_operations", None)
        if list_operations is not None:
            candidates = [
                candidate
                for candidate in list_operations(limit=100)
                if (
                    candidate.artifact_set_sha256 == artifact_set_sha256
                    and candidate.state
                    in {*model_cache_states.ACTIVE, "succeeded", "failed"}
                    and not (candidate.state == "succeeded" and new_bytes > 0)
                )
            ]
            state_rank = {
                "succeeded": 0,
                "queued": 1,
                "running": 1,
                model_cache_states.BACKOFF: 1,
                "failed": 2,
            }
            operation = min(
                candidates,
                key=lambda candidate: (
                    state_rank.get(candidate.state, 3),
                    str(candidate.id),
                ),
                default=None,
            )
            if operation is not None and operation.state == "failed":
                failure = _read(AvailabilityOperationFailure, operation.failure)
                actions = failure.recovery_actions if failure else []
                if "download_again" in actions:
                    operation = self._start_model_repair(
                        operation,
                        actor=actor,
                        parent_request_key=parent_request_key,
                    )
        if operation is None:
            operation = self._model_cache.start_download(
                actor=actor,
                request_key=child_request_key,
                plan_digest=plan_digest,
                recipe_revision_id=recipe_revision_id,
            )
    except RecipeImageAvailabilityError:
        raise
    except Exception as error:
        raise _ModelQueueFailed(
            error, "exact Model artifact preparation could not be queued"
        ) from error
    return AvailabilityModelChild.model_validate_json(
        canonical_message(
            {
                "id": operation.id,
                "request_key": str(
                    getattr(operation, "request_key", child_request_key)
                ),
                "state": operation.state,
                "artifact_set_sha256": artifact_set_sha256,
                "plan_digest": plan_digest,
                "model_content_digests": manifest_document.model_content_digests,
                "artifacts": [
                    artifact.model_dump(mode="json")
                    for item in manifest_document.artifacts
                    if (
                        artifact := _read(
                            RecipeImageAvailabilityArtifact,
                            item.model_dump(mode="json"),
                        )
                    )
                    is not None
                ],
                "progress": self._model_progress(operation.progress).model_dump(
                    mode="json"
                ),
            }
        )
    )


def _start_model_repair(
    self: RecipeImageAvailabilityService,
    operation: object,
    *,
    actor: str,
    parent_request_key: str,
) -> ModelCacheOperationHandle:
    """Start a fresh content-addressed repair without deleting valid bytes."""

    artifact_set_sha256 = getattr(operation, "artifact_set_sha256", None)
    if not isinstance(artifact_set_sha256, str):
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.MODEL_CACHE_INVALID,
            "integrity failure did not retain an artifact-set identity",
            retryable=True,
            recovery_actions=("retry",),
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    repair_preview = getattr(self._model_cache, "repair_preview", None)
    start_repair = getattr(self._model_cache, "start_repair", None)
    if repair_preview is None or start_repair is None:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.MODEL_CACHE_UNAVAILABLE,
            "ModelCache does not expose the canonical repair workflow",
            retryable=True,
            recovery_actions=("retry",),
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    preview = _read(
        ModelCacheRepairPreviewResponse, repair_preview(artifact_set_sha256)
    )
    plan_digest = preview.plan_digest if preview else None
    if not isinstance(plan_digest, str):
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.MODEL_CACHE_INVALID,
            "ModelCache returned an incomplete repair plan",
            retryable=True,
            recovery_actions=("retry",),
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    request_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-availability-model-repair:{artifact_set_sha256}:{parent_request_key}",
        )
    )
    return start_repair(
        actor=actor,
        request_key=request_key,
        artifact_set_sha256=artifact_set_sha256,
        plan_digest=plan_digest,
    )


def _resume_model_child(
    self: RecipeImageAvailabilityService,
    child: AvailabilityModelChild | None,
    *,
    actor: str,
    parent_request_key: str,
) -> AvailabilityModelChild | None:
    if self._model_cache is None or child is None:
        return child
    child_id = child.id
    try:
        operation = self._model_cache.get_operation(child_id)
        if operation.state == "failed":
            reused = None
            list_operations = getattr(self._model_cache, "list_operations", None)
            if list_operations is not None:
                candidates = [
                    candidate
                    for candidate in list_operations(limit=100)
                    if (
                        candidate.id != child_id
                        and candidate.artifact_set_sha256
                        == operation.artifact_set_sha256
                        and candidate.state in {*model_cache_states.ACTIVE, "succeeded"}
                    )
                ]
                state_rank = {
                    "succeeded": 0,
                    "queued": 1,
                    "running": 1,
                    model_cache_states.BACKOFF: 1,
                }
                reused = min(
                    candidates,
                    key=lambda candidate: (
                        state_rank.get(candidate.state, 2),
                        str(candidate.id),
                    ),
                    default=None,
                )
            if reused is not None:
                operation = reused
            else:
                retry_key = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:recipe-availability-model-retry:{child_id}:{parent_request_key}",
                    )
                )
                failure = _read(AvailabilityOperationFailure, operation.failure)
                actions = failure.recovery_actions if failure else []
                if "download_again" in actions and isinstance(
                    operation.artifact_set_sha256, str
                ):
                    operation = self._start_model_repair(
                        operation,
                        actor=actor,
                        parent_request_key=parent_request_key,
                    )
                elif (
                    "check_access_and_resume" in actions
                    and isinstance(operation.artifact_set_sha256, str)
                    and isinstance(operation.plan_digest, str)
                    and callable(
                        getattr(self._model_cache, "check_access_and_resume", None)
                    )
                ):
                    operation = self._model_cache.check_access_and_resume(
                        child_id,
                        actor=actor,
                        request_key=retry_key,
                        artifact_set_sha256=operation.artifact_set_sha256,
                        plan_digest=operation.plan_digest,
                    )
                else:
                    operation = self._model_cache.retry(
                        child_id, actor=actor, request_key=retry_key
                    )
        return self._refresh_model_observation(child, operation)
    except Exception as error:
        raise _ModelQueueFailed(
            error, "Model artifact operation could not be resumed"
        ) from error
