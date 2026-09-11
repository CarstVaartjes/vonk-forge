"""Authenticated schema-2 HTTP routes for the Controller NAS model cache."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Request, status
from vonk_agent_protocol import OperationProgress

from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor
from .bounded_json import require_integer, require_sequence
from .model_cache import (
    ModelCacheConflict,
    ModelCacheError,
    ModelCacheNotFound,
    ModelCacheResolutionError,
    ModelCacheService,
)
from .model_cache_contract import (
    ModelCacheOperatorAction,
    ModelCacheOperatorRequest,
    ModelCacheOperatorResponse,
    ModelCacheRemovalResult,
)
from .model_cache_progress import project_cache_progress
from .operation_api import (
    OperationApiServices,
    OperationListPage,
    OperationProjectionError,
    OperationProvider,
    bounded_error_responses,
)
from .operation_contract import AvailabilityOperationFailure

MODEL_CACHE_OPERATION_IDS = {
    ("get", "/api/model/operations/{operation_id}"): "getModelOperation",
    ("post", "/api/model/{selector}/download"): "downloadModel",
    ("post", "/api/model/{selector}/remove"): "removeModel",
}

def _model_operator_response(
    operation: Any, *, action: ModelCacheOperatorAction, selector: str
) -> ModelCacheOperatorResponse:
    raw = project_cache_progress(operation.progress)
    progress = OperationProgress.model_validate(raw)
    result = operation.result
    cancelled = (
        result.cancelled_operations
        if isinstance(result, ModelCacheRemovalResult)
        else []
    )
    return ModelCacheOperatorResponse(
        action=action,
        selector=selector,
        request_key=operation.request_key,
        operation_id=operation.id,
        state=operation.state,
        phase=str(raw.get("phase", progress.phase)),
        progress=progress,
        transferred_bytes=progress.completed_bytes,
        total_bytes=progress.total_bytes,
        eta_seconds=progress.eta_seconds,
        preserved=(
            ["verified-controller-copy"]
            if action == "download" and operation.state == "succeeded"
            else []
        ),
        next_actions=(
            ["retry"]
            if operation.state == "failed" and operation.retryable
            else []
        ),
        cancelled_operations=list(cancelled),
        result=result,
        failure=(
            AvailabilityOperationFailure.model_validate(operation.failure)
            if isinstance(operation.failure, Mapping)
            else None
        ),
    )


def install_model_operator_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: ModelCacheService | None,
    audits: Any,
) -> None:
    """Install the current singular Model mutation routes.

    The service resolves selectors and binds its own current preview.  The
    operator therefore submits only intent and a request identity.
    """

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(MODEL_CACHE_OPERATION_IDS)

    def cache() -> ModelCacheService:
        if service is None:
            raise HTTPException(status_code=503, detail="model cache unavailable")
        return service

    def require_operator(actor: Actor, route: str) -> None:
        if actor.role not in MUTATION_ROLES[("POST", route)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    def audit(request: Request, actor: Actor, action: str, selector: str, operation_id: str) -> None:
        if audits is not None:
            audits.append(
                AuditRecord(
                    request.state.request_id,
                    actor.subject,
                    action,
                    None,
                    (selector, operation_id),
                )
            )

    def failure(error: BaseException) -> HTTPException:
        if isinstance(error, ModelCacheNotFound):
            return HTTPException(status_code=404, detail=error.detail)
        if isinstance(error, ModelCacheConflict):
            return HTTPException(status_code=409, detail=error.detail)
        if isinstance(error, ModelCacheResolutionError):
            return HTTPException(status_code=422, detail=error.detail)
        return HTTPException(status_code=503, detail="model cache unavailable")

    @app.get(
        "/api/model/operations/{operation_id}",
        response_model=ModelCacheOperatorResponse,
        responses=bounded_error_responses(401, 404, 409, 422, 503),
        operation_id="getModelOperation",
    )
    def get_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=128)],
        actor: Actor = actor_dependency,
    ) -> ModelCacheOperatorResponse:
        """Observe a submitted model mutation; any authenticated actor may read it."""

        del actor
        try:
            operation, action, selector = cache().get_operator_operation(operation_id)
            return _model_operator_response(operation, action=action, selector=selector)
        except HTTPException:
            raise
        except (ModelCacheError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise failure(error) from None

    @app.post(
        "/api/model/{selector}/download",
        response_model=ModelCacheOperatorResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="downloadModel",
    )
    def download(
        body: ModelCacheOperatorRequest,
        request: Request,
        selector: Annotated[str, Path(min_length=1, max_length=256)],
        actor: Actor = actor_dependency,
    ) -> ModelCacheOperatorResponse:
        require_operator(actor, "/api/model/{selector}/download")
        try:
            operation = cache().download_model_selector(
                selector,
                actor=actor.subject,
                request_key=body.request_key,
                force=True,
            )
            audit(request, actor, "model.download", selector, operation.id)
            return _model_operator_response(operation, action="download", selector=selector)
        except HTTPException:
            raise
        except (ModelCacheError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise failure(error) from None

    @app.post(
        "/api/model/{selector}/remove",
        response_model=ModelCacheOperatorResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="removeModel",
    )
    def remove(
        body: ModelCacheOperatorRequest,
        request: Request,
        selector: Annotated[str, Path(min_length=1, max_length=256)],
        actor: Actor = actor_dependency,
    ) -> ModelCacheOperatorResponse:
        require_operator(actor, "/api/model/{selector}/remove")
        try:
            operation = cache().remove_model_selector(
                selector,
                actor=actor.subject,
                request_key=body.request_key,
            )
            audit(request, actor, "model.remove", selector, operation.id)
            return _model_operator_response(operation, action="remove", selector=selector)
        except HTTPException:
            raise
        except (ModelCacheError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise failure(error) from None


class ModelCacheOperationProvider:
    """Current Activity provider for the Controller-owned cache family."""

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
        items = [
            self._summary(item)
            for item in require_sequence(page["operations"], "page operations")
        ]
        next_cursor = self._next_cursor(
            page.get("_next_boundary"), state=state, node_id=node_id
        )
        return OperationListPage(
            items=items,
            next_cursor=next_cursor,
            total=require_integer(page["total"], "page total"),
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
        raise OperationProjectionError("operation cursor projection unavailable")

    def get_operation(self, operation_id: str) -> dict[str, object]:
        try:
            operation = self._service.get_operation(operation_id)
        except ModelCacheNotFound:
            # A global Activity lookup asks every family. An absent cache
            # operation must let another family supply the requested operation.
            raise KeyError(operation_id) from None
        return self._summary(operation)

    def _summary(self, operation: Any) -> dict[str, object]:
        progress = dict(operation.progress)
        result = (
            None if operation.result is None else operation.result.model_dump(mode="json")
        )
        retryable = operation.state == "failed" and operation.retryable
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
            "failure": operation.failure,
        }

    @staticmethod
    def _progress(value: Mapping[str, object]) -> dict[str, object]:
        return project_cache_progress(value)

def model_cache_operation_provider(
    service: ModelCacheService,
    cursors: Any | None = None,
) -> OperationProvider:
    provider = ModelCacheOperationProvider(service, cursors)
    return OperationProvider(
        family=provider.family,
        list_operations=provider.list_operations,
        get_operation=provider.get_operation,
    )


def register_model_cache_operation_provider(
    services: OperationApiServices | None, service: ModelCacheService | None
) -> OperationApiServices | None:
    """Attach the cache family to the current shared Activity services."""
    if services is None or service is None:
        return services
    provider = model_cache_operation_provider(
        service, services.cursor_codec
    )
    existing = services.operation_providers
    if any(item.family == "model-cache" for item in existing):
        return services
    from dataclasses import replace

    return replace(services, operation_providers=existing + (provider,))


__all__ = [
    "MODEL_CACHE_OPERATION_IDS",
    "ModelCacheOperationProvider",
    "install_model_operator_routes",
    "model_cache_operation_provider",
    "register_model_cache_operation_provider",
]
