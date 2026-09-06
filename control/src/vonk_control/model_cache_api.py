"""Authenticated schema-2 HTTP routes for the Controller NAS model cache."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query, Request, status

from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor, CursorCodec
from .model_cache import (
    ModelCacheConflict,
    ModelCacheError,
    ModelCacheNotFound,
    ModelCacheResolutionError,
    ModelCacheService,
)
from .model_cache_contract import (
    CacheEntryResponse,
    ModelCacheDownloadPreviewRequest,
    ModelCacheDownloadPreviewResponse,
    ModelCacheDownloadRequest,
    ModelCacheAccessResumeRequest,
    ModelCacheEvictionPreviewRequest,
    ModelCacheEvictionPreviewResponse,
    ModelCacheEvictRequest,
    ModelCacheInventoryResponse,
    ModelCacheOperationResponse,
    ModelCacheOperationsResponse,
    ModelCacheRepairPreviewRequest,
    ModelCacheRepairPreviewResponse,
    ModelCacheRepairRequest,
    ModelCacheRetryRequest,
    ModelCacheUpdatesResponse,
)
from .operation_api import bounded_error_responses
from .operation_contract import AvailabilityOperationFailure

MODEL_CACHE_OPERATION_IDS = {
    ("get", "/api/v1/model-cache"): "getModelCacheInventory",
    ("get", "/api/v1/model-cache/entries/{artifact_set_sha256}"):
        "getModelCacheEntry",
    ("post", "/api/v1/model-cache/download-preview"):
        "previewModelCacheDownload",
    ("post", "/api/v1/model-cache/download"): "downloadModelCache",
    ("post", "/api/v1/model-cache/repair-preview"): "previewModelCacheRepair",
    ("post", "/api/v1/model-cache/repair"): "repairModelCache",
    ("post", "/api/v1/model-cache/eviction-preview"):
        "previewModelCacheEviction",
    ("post", "/api/v1/model-cache/evict"): "evictModelCache",
    ("get", "/api/v1/model-cache/updates"): "getModelCacheUpdates",
    ("get", "/api/v1/model-cache/operations"): "listModelCacheOperations",
    ("get", "/api/v1/model-cache/operations/{operation_id}"):
        "getModelCacheOperation",
    ("post", "/api/v1/model-cache/operations/{operation_id}/retry"):
        "retryModelCacheOperation",
    ("post", "/api/v1/model-cache/operations/{operation_id}/check-access-and-resume"):
        "checkModelCacheAccessAndResume",
}

_DIGEST = r"^[0-9a-f]{64}$"
_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def install_model_cache_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: ModelCacheService | None,
    audits: Any,
    cursors: CursorCodec | None = None,
) -> None:
    """Install the cache routes without exposing fixture transport inputs."""

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(MODEL_CACHE_OPERATION_IDS)
    authenticated = actor_dependency

    def cache() -> ModelCacheService:
        if service is None:
            raise HTTPException(status_code=503, detail="model cache unavailable")
        return service

    def require_mutation(actor: Actor, method: str, route: str) -> None:
        if actor.role not in MUTATION_ROLES[(method, route)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    def audit(request: Request, actor: Actor, action: str, *targets: str) -> None:
        if audits is not None:
            audits.append(
                AuditRecord(
                    request.state.request_id,
                    actor.subject,
                    action,
                    None,
                    tuple(targets),
                )
            )

    def error(error: BaseException, unavailable: str) -> HTTPException:
        if isinstance(error, ModelCacheNotFound):
            return HTTPException(status_code=404, detail=error.detail)
        if isinstance(error, ModelCacheConflict):
            return HTTPException(status_code=409, detail=error.detail)
        if isinstance(error, ModelCacheResolutionError):
            return HTTPException(status_code=422, detail=error.detail)
        if isinstance(error, ModelCacheError):
            return HTTPException(status_code=503, detail=unavailable)
        return HTTPException(status_code=503, detail=unavailable)

    def operation_response(operation: Any) -> ModelCacheOperationResponse:
        result = None if operation.result is None else dict(operation.result)
        failure = None
        raw_failure = getattr(operation, "failure", None)
        if isinstance(raw_failure, Mapping):
            failure = AvailabilityOperationFailure.model_validate(raw_failure)
        return ModelCacheOperationResponse.model_validate(
            {
                "schema_version": 2,
                "id": operation.id,
                "request_key": operation.request_key,
                "kind": operation.kind,
                "state": operation.state,
                "attempt": operation.attempt,
                "artifact_set_sha256": operation.artifact_set_sha256,
                "plan_digest": operation.plan_digest,
                "progress": dict(operation.progress),
                "result": result,
                "failure": failure,
                "created_at": operation.created_at,
                "updated_at": operation.updated_at,
                "completed_at": operation.completed_at,
            }
        )

    def decode_cursor(
        cursor: str | None,
        *,
        resource: str,
        order: str,
        context: dict[str, object],
    ) -> tuple[str, str] | None:
        if cursor is None:
            return None
        if cursors is None:
            raise HTTPException(status_code=422, detail="cache cursor is invalid")
        try:
            value = cursors.decode(
                cursor, resource=resource, order=order, context=context
            )
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not all(isinstance(item, str) for item in value)
            ):
                raise ValueError
            return value[0], value[1]
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="cache cursor is invalid") from None

    def encode_cursor(
        boundary: tuple[str, str] | None,
        *,
        resource: str,
        order: str,
        context: dict[str, object],
    ) -> str | None:
        if boundary is None:
            return None
        if cursors is None:
            raise HTTPException(status_code=503, detail="cache pagination unavailable")
        return cursors.encode(
            resource=resource,
            order=order,
            context=context,
            boundary=list(boundary),
        )

    @app.get(
        "/api/v1/model-cache",
        response_model=ModelCacheInventoryResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="getModelCacheInventory",
    )
    def get_inventory(
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        _actor: Actor = authenticated,
    ) -> ModelCacheInventoryResponse:
        try:
            context = {"limit": limit}
            boundary = decode_cursor(
                cursor,
                resource="model-cache-inventory",
                order="updated-at-desc/digest-desc/v1",
                context=context,
            )
            result = cache().inventory(limit=limit, boundary=boundary)
            return {
                "schema_version": 2,
                "source_policy": "nas-first",
                "entries": result["entries"],
                "storage": result["storage"],
                "total": result["total"],
                "next_cursor": encode_cursor(
                    result.get("_next_boundary"),
                    resource="model-cache-inventory",
                    order="updated-at-desc/digest-desc/v1",
                    context=context,
                ),
            }
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache inventory unavailable") from None

    @app.get(
        "/api/v1/model-cache/entries/{artifact_set_sha256}",
        response_model=CacheEntryResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getModelCacheEntry",
    )
    def get_entry(
        artifact_set_sha256: Annotated[str, Path(pattern=_DIGEST)],
        _actor: Actor = authenticated,
    ) -> CacheEntryResponse:
        try:
            return cache().get_entry(artifact_set_sha256)
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache entry unavailable") from None

    @app.post(
        "/api/v1/model-cache/download-preview",
        response_model=ModelCacheDownloadPreviewResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewModelCacheDownload",
    )
    def preview_download(
        body: ModelCacheDownloadPreviewRequest,
        actor: Actor = authenticated,
    ) -> ModelCacheDownloadPreviewResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/download-preview")
        try:
            result = cache().download_preview(
                artifact_set_sha256=body.artifact_set_sha256,
                model_version_sha256=body.model_version_sha256,
                recipe_revision_sha256=body.recipe_revision_sha256,
                recipe_revision_id=body.recipe_revision_id,
            )
            return {
                key: value for key, value in result.items() if not key.startswith("_")
            }
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache download preview unavailable") from None

    @app.post(
        "/api/v1/model-cache/download",
        response_model=ModelCacheOperationResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="downloadModelCache",
    )
    def download(
        request: Request,
        body: ModelCacheDownloadRequest,
        actor: Actor = authenticated,
    ) -> ModelCacheOperationResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/download")
        try:
            result = cache().start_download(
                actor=actor.subject,
                request_key=body.request_key,
                plan_digest=body.plan_digest,
                artifact_set_sha256=body.artifact_set_sha256,
                model_version_sha256=body.model_version_sha256,
                recipe_revision_sha256=body.recipe_revision_sha256,
                recipe_revision_id=body.recipe_revision_id,
            )
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache download unavailable") from None
        audit(request, actor, "model-cache.download", result.id, result.artifact_set_sha256 or "")
        return operation_response(result)

    @app.post(
        "/api/v1/model-cache/repair-preview",
        response_model=ModelCacheRepairPreviewResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewModelCacheRepair",
    )
    def preview_repair(
        body: ModelCacheRepairPreviewRequest,
        actor: Actor = authenticated,
    ) -> ModelCacheRepairPreviewResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/repair-preview")
        try:
            return cache().repair_preview(body.artifact_set_sha256)
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache repair preview unavailable") from None

    @app.post(
        "/api/v1/model-cache/repair",
        response_model=ModelCacheOperationResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="repairModelCache",
    )
    def repair(
        request: Request,
        body: ModelCacheRepairRequest,
        actor: Actor = authenticated,
    ) -> ModelCacheOperationResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/repair")
        try:
            result = cache().start_repair(
                actor=actor.subject,
                request_key=body.request_key,
                artifact_set_sha256=body.artifact_set_sha256,
                plan_digest=body.plan_digest,
            )
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache repair unavailable") from None
        audit(request, actor, "model-cache.repair", result.id, result.artifact_set_sha256 or "")
        return operation_response(result)

    @app.post(
        "/api/v1/model-cache/eviction-preview",
        response_model=ModelCacheEvictionPreviewResponse,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="previewModelCacheEviction",
    )
    def preview_eviction(
        body: ModelCacheEvictionPreviewRequest,
        actor: Actor = authenticated,
    ) -> ModelCacheEvictionPreviewResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/eviction-preview")
        try:
            result = cache().eviction_preview(target_bytes=body.target_bytes)
            return {
                key: value
                for key, value in result.items()
                if not key.startswith("_") and key != "selected_objects"
            }
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache eviction preview unavailable") from None

    @app.post(
        "/api/v1/model-cache/evict",
        response_model=ModelCacheOperationResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="evictModelCache",
    )
    def evict(
        request: Request,
        body: ModelCacheEvictRequest,
        actor: Actor = authenticated,
    ) -> ModelCacheOperationResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/evict")
        try:
            result = cache().evict(
                actor=actor.subject,
                request_key=body.request_key,
                plan_digest=body.plan_digest,
                target_bytes=body.target_bytes,
            )
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache eviction unavailable") from None
        audit(request, actor, "model-cache.evict", result.id)
        return operation_response(result)

    @app.get(
        "/api/v1/model-cache/updates",
        response_model=ModelCacheUpdatesResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="getModelCacheUpdates",
    )
    def get_updates(
        artifact_set_sha256: Annotated[str | None, Query(pattern=_DIGEST)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        _actor: Actor = authenticated,
    ) -> ModelCacheUpdatesResponse:
        try:
            context = {"limit": limit, "artifact_set_sha256": artifact_set_sha256}
            boundary = decode_cursor(
                cursor,
                resource="model-cache-updates",
                order="updated-at-desc/digest-desc/v1",
                context=context,
            )
            result = cache().discover_updates(
                artifact_set_sha256=artifact_set_sha256,
                limit=limit,
                boundary=boundary,
            )
            return {
                "schema_version": 2,
                "source_policy": "nas-first",
                "updates": result["updates"],
                "total": result["total"],
                "next_cursor": encode_cursor(
                    result.get("_next_boundary"),
                    resource="model-cache-updates",
                    order="updated-at-desc/digest-desc/v1",
                    context=context,
                ),
            }
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache updates unavailable") from None

    @app.get(
        "/api/v1/model-cache/operations",
        response_model=ModelCacheOperationsResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="listModelCacheOperations",
    )
    def list_operations(
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        _actor: Actor = authenticated,
    ) -> ModelCacheOperationsResponse:
        try:
            context = {"limit": limit}
            boundary = decode_cursor(
                cursor,
                resource="model-cache-operations",
                order="created-at-desc/id-desc/v1",
                context=context,
            )
            result = cache().operations_page(limit=limit, boundary=boundary)
            return {
                "schema_version": 2,
                "operations": [
                    operation_response(item) for item in result["operations"]
                ],
                "total": result["total"],
                "next_cursor": encode_cursor(
                    result.get("_next_boundary"),
                    resource="model-cache-operations",
                    order="created-at-desc/id-desc/v1",
                    context=context,
                ),
            }
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache operations unavailable") from None

    @app.get(
        "/api/v1/model-cache/operations/{operation_id}",
        response_model=ModelCacheOperationResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getModelCacheOperation",
    )
    def get_operation(
        operation_id: Annotated[str, Path(pattern=_UUID)],
        _actor: Actor = authenticated,
    ) -> ModelCacheOperationResponse:
        try:
            return operation_response(cache().get_operation(operation_id))
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache operation unavailable") from None

    @app.post(
        "/api/v1/model-cache/operations/{operation_id}/retry",
        response_model=ModelCacheOperationResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="retryModelCacheOperation",
    )
    def retry_operation(
        body: ModelCacheRetryRequest,
        request: Request,
        operation_id: Annotated[str, Path(pattern=_UUID)],
        actor: Actor = authenticated,
    ) -> ModelCacheOperationResponse:
        require_mutation(actor, "POST", "/api/v1/model-cache/operations/{operation_id}/retry")
        try:
            result = cache().retry(
                operation_id,
                actor=actor.subject,
                request_key=body.request_key,
            )
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache operation retry unavailable") from None
        audit(request, actor, "model-cache.retry", operation_id, result.id)
        return operation_response(result)

    @app.post(
        "/api/v1/model-cache/operations/{operation_id}/check-access-and-resume",
        response_model=ModelCacheOperationResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="checkModelCacheAccessAndResume",
    )
    def check_access_and_resume(
        body: ModelCacheAccessResumeRequest,
        request: Request,
        operation_id: Annotated[str, Path(pattern=_UUID)],
        actor: Actor = authenticated,
    ) -> ModelCacheOperationResponse:
        require_mutation(
            actor,
            "POST",
            "/api/v1/model-cache/operations/{operation_id}/check-access-and-resume",
        )
        try:
            result = cache().check_access_and_resume(
                operation_id,
                actor=actor.subject,
                request_key=body.request_key,
                artifact_set_sha256=body.artifact_set_sha256,
                plan_digest=body.plan_digest,
            )
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error(exc, "model cache access recheck unavailable") from None
        audit(
            request,
            actor,
            "model-cache.check-access-and-resume",
            operation_id,
            result.id,
        )
        return operation_response(result)

class ModelCacheOperationProvider:
    """Duck-typed Activity provider for the Controller-owned cache family."""

    family = "model-cache"

    def __init__(self, service: ModelCacheService, cursors: Any | None = None) -> None:
        self._service = service
        self._cursors = cursors

    def list_operations(self, query: Any = None) -> Any:
        limit = int(getattr(query, "limit", 100) or 100)
        after = getattr(query, "after", None)
        state = getattr(query, "state", None)
        node_id = getattr(query, "node_id", None)
        page = self._service.activity_operations(
            after=after,
            limit=min(limit, 101),
            state=state,
            node_id=node_id,
        )
        items = [self._summary(item) for item in page["operations"]]
        next_cursor = self._next_cursor(
            page.get("_next_boundary"), state=state, node_id=node_id
        )
        # The typed provider classes landed after the cache slice's base
        # revision.  Keep this module importable on that revision while
        # returning the exact dataclass expected once the shared seam is
        # present.
        try:
            from .operation_api import OperationListPage
        except ImportError:
            return {
                "items": items,
                "next_cursor": next_cursor,
                "total": page["total"],
            }
        return OperationListPage(
            items=items,
            next_cursor=next_cursor,
            total=int(page["total"]),
        )

    def _next_cursor(
        self,
        boundary: object,
        *,
        state: object,
        node_id: object,
    ) -> str | None:
        if not isinstance(boundary, tuple) or len(boundary) != 2:
            return None
        created_at, operation_id = boundary
        if not isinstance(created_at, str) or not isinstance(operation_id, str):
            return None
        context = {"state": state, "node_id": node_id}
        if self._cursors is not None:
            return self._cursors.encode(
                resource="model-cache-operations",
                order="created-at-desc/id-desc/v1",
                context=context,
                boundary=[created_at, operation_id],
            )
        # This fallback is only used by the pre-Activity base revision.  The
        # production composition always supplies the shared signed codec.
        return f"{created_at}|{operation_id}"[:1024]

    def get_operation(self, operation_id: str) -> dict[str, object]:
        return self._summary(self._service.get_operation(operation_id))

    def _summary(self, operation: Any) -> dict[str, object]:
        progress = dict(operation.progress)
        result = (
            None if operation.result is None else dict(operation.result)
        )
        retryable = operation.state == "failed" and operation.retryable
        if operation.state == "failed" and result is None:
            result = {
                "error_code": "model_cache_operation_failed",
                "summary": f"Model cache {operation.kind} failed",
                "detail": (
                    None
                    if operation.last_error is None
                    else operation.last_error[:256]
                ),
                "retryable": retryable,
                "uncertain": False,
            }
        elif operation.state == "failed" and result is not None:
            result.setdefault("retryable", retryable)
            result.setdefault("uncertain", False)
        return {
            "id": operation.id,
            "parent_id": None,
            "node_ids": [],
            "kind": f"model-cache.{operation.kind}",
            "state": operation.state,
            "attempt": operation.attempt,
            "progress": self._progress(progress),
            "created_at": operation.created_at,
            "updated_at": operation.updated_at,
            "supported_actions": ["retry"] if retryable else [],
            "result": result,
        }

    @staticmethod
    def _progress(value: Mapping[str, object]) -> dict[str, object]:
        """Map cache-specific counters to the strict global progress shape."""
        raw_phase = value.get("phase")
        phase = {
            "queued": "prepare",
            "downloading": "download",
            "verifying": "verify",
            "reclaiming": "cleanup",
            "completed": "final_verify",
            "failed": "final_verify",
        }.get(raw_phase, raw_phase)
        if not isinstance(phase, str) or not phase.strip() or len(phase) > 80:
            phase = "prepare"
        progress: dict[str, object] = {"phase": phase}
        completed = value.get("downloaded_bytes")
        total = value.get("expected_bytes")
        if isinstance(completed, int) and not isinstance(completed, bool) and completed >= 0:
            progress["completed_bytes"] = completed
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            progress["total_bytes"] = total
            progress["total_bytes_known"] = True
        else:
            progress["total_bytes_known"] = False
        completed_items = value.get("completed_artifacts")
        current_key = value.get("current_artifact_key")
        if isinstance(completed_items, int) and not isinstance(completed_items, bool) and completed_items >= 0:
            checkpoint: dict[str, object] = {
                "key": "artifact-set",
                "sequence": completed_items,
            }
            if isinstance(current_key, str) and current_key:
                checkpoint["cursor"] = current_key[:512]
            progress["checkpoint"] = checkpoint
        return progress

def model_cache_operation_provider(
    service: ModelCacheService,
    cursors: Any | None = None,
) -> Any:
    provider = ModelCacheOperationProvider(service, cursors)
    try:
        from .operation_api import OperationProvider
    except ImportError:
        return provider
    return OperationProvider(
        family=provider.family,
        list_operations=provider.list_operations,
        get_operation=provider.get_operation,
    )


def register_model_cache_operation_provider(
    services: Any, service: ModelCacheService | None
) -> Any:
    """Attach the cache family when the shared Activity seam is available.

    The cache slice remains importable against the pre-Activity base commit;
    integration branches expose ``operation_providers`` on the immutable
    ``OperationApiServices`` value.  This adapter keeps registration at the
    production composition boundary without importing or constructing the app.
    """
    if services is None or service is None or not hasattr(services, "operation_providers"):
        return services
    provider = model_cache_operation_provider(
        service, getattr(services, "cursor_codec", None)
    )
    existing = tuple(getattr(services, "operation_providers", ()))
    if any(getattr(item, "family", None) == "model-cache" for item in existing):
        return services
    from dataclasses import replace

    return replace(services, operation_providers=existing + (provider,))


__all__ = [
    "MODEL_CACHE_OPERATION_IDS",
    "ModelCacheOperationProvider",
    "install_model_cache_routes",
    "model_cache_operation_provider",
    "register_model_cache_operation_provider",
]
