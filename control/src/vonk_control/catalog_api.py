"""Strict authenticated HTTP surface for the local database recipe catalog."""

from __future__ import annotations

import uuid
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import ConfigDict, Field
from starlette.responses import JSONResponse

from .audit import AuditRecord
from .auth import Actor
from .catalog_service import (
    CatalogConflict,
    CatalogError,
    CatalogService,
)
from .catalog_sync import CatalogSyncError, CatalogSyncView
from .catalog_sync_contract import (
    ManagedCatalogStaleRecipe,
    ManagedCatalogSyncProblem,
    ManagedCatalogWithdrawnRecipe,
)
from .library_contract import UuidId
from .recipe_library_types import RecipeLibraryError
from .strict_json import StrictJSONModel

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SEMVER = (
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_RECIPE_LIBRARY_UNAVAILABLE_CODES = frozenset(
    {
        "recipe_library.unavailable",
        "recipe_library.transient",
        "recipe_package.unavailable",
        "recipe_package.transient",
    }
)

CATALOG_OPERATION_IDS = {
    (
        "post",
        "/api/catalog/managed-recipes/sync",
    ): "syncManagedRecipeCatalog",
    (
        "get",
        "/api/catalog/managed-recipes/sync-status",
    ): "getManagedRecipeCatalogSyncStatus",
}


class AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...


class ManagedRecipeCatalogSync(Protocol):
    def sync(
        self,
        *,
        request_key: str,
        trigger: str,
        actor: str,
        expected_commit: str | None = None,
    ) -> CatalogSyncView: ...

    def latest(self) -> CatalogSyncView | None: ...


class StrictModel(StrictJSONModel):
    # API JSON is a typed boundary.  Pydantic's default lax mode would turn
    # values such as ``1`` into ``"1"`` and accept integer flags as booleans,
    # which makes malformed requests indistinguishable from canonical ones.
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CatalogProblem(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    request_id: UuidId


class ManagedCatalogSyncRequest(StrictModel):
    request_key: UuidId = Field(default_factory=lambda: str(uuid.uuid4()))
    expected_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class ManagedCatalogSyncFailure(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    occurred_at: str


class ManagedCatalogSyncResponse(StrictModel):
    schema_version: Literal[1] = 1
    sync_id: UuidId
    request_key: UuidId
    trigger: Literal["manual", "automatic"]
    state: Literal["syncing", "current", "partial", "failed"]
    repository: str = Field(min_length=1, max_length=200)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    total_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    imported_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    withdrawn_count: int = Field(ge=0)
    withdrawn_recipes: list[ManagedCatalogWithdrawnRecipe]
    stale_recipes: list[ManagedCatalogStaleRecipe]
    problems: list[ManagedCatalogSyncProblem]
    created_at: str
    completed_at: str | None
    last_error: ManagedCatalogSyncFailure | None = None


def _catalog_problem(
    request: Request, *, status_code: int, code: str, detail: str
) -> JSONResponse:
    problem = CatalogProblem(
        code=code[:128],
        detail=detail[:256],
        request_id=request.state.request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
    )


def _problem(request: Request, error: CatalogError) -> JSONResponse:
    status_code = 409 if isinstance(error, CatalogConflict) else 422
    return _catalog_problem(
        request,
        status_code=status_code,
        code=error.code,
        detail=error.detail,
    )


def _recipe_library_problem(
    request: Request, error: RecipeLibraryError
) -> JSONResponse:
    """Map bounded reader failures to the managed-sync HTTP contract."""
    status_code = 503 if error.code in _RECIPE_LIBRARY_UNAVAILABLE_CODES else 422
    return _catalog_problem(
        request,
        status_code=status_code,
        code=error.code,
        detail=error.detail,
    )


def _managed_sync(value: CatalogSyncView) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sync_id": value.id,
        "request_key": value.request_key,
        "trigger": value.trigger,
        "state": value.state,
        "repository": value.repository,
        "commit": value.commit,
        "expected_commit": value.expected_commit,
        "total_count": value.total_count,
        "processed_count": value.processed_count,
        "imported_count": value.imported_count,
        "updated_count": value.updated_count,
        "unchanged_count": value.unchanged_count,
        "skipped_count": value.skipped_count,
        "withdrawn_count": value.withdrawn_count,
        "withdrawn_recipes": list(value.withdrawn_recipes),
        "stale_recipes": list(value.stale_recipes),
        "problems": list(value.problems),
        "created_at": value.created_at.isoformat(),
        "completed_at": (
            value.completed_at.isoformat() if value.completed_at is not None else None
        ),
        "last_error": (
            {
                "code": value.last_error.code,
                "detail": value.last_error.detail,
                "occurred_at": value.last_error.occurred_at.isoformat(),
            }
            if value.last_error is not None
            else None
        ),
    }


def install_catalog_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    service: CatalogService | None,
    managed_sync: ManagedRecipeCatalogSync | None = None,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(CATALOG_OPERATION_IDS)
    authenticated = actor_dependency

    def catalog() -> CatalogService:
        if service is None:
            raise HTTPException(status_code=503, detail="catalog unavailable")
        return service

    def administrator(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def sync_service() -> ManagedRecipeCatalogSync:
        if managed_sync is None:
            raise HTTPException(
                status_code=503, detail="managed recipe catalog sync is unavailable"
            )
        return managed_sync

    @app.post(
        "/api/catalog/managed-recipes/sync",
        response_model=ManagedCatalogSyncResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
            503: {"model": CatalogProblem},
        },
        operation_id="syncManagedRecipeCatalog",
    )
    def sync_managed_recipe_catalog(
        body: ManagedCatalogSyncRequest,
        request: Request,
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = sync_service().sync(
                request_key=body.request_key,
                trigger="manual",
                actor=actor.subject,
                expected_commit=body.expected_commit,
            )
        except CatalogSyncError as error:
            return _catalog_problem(
                request,
                status_code=(
                    409
                    if error.code
                    in {
                        "catalog.sync_in_progress",
                        "catalog.sync_preview_changed",
                        "catalog.sync_request_reused",
                    }
                    else 422
                ),
                code=error.code,
                detail=error.detail,
            )
        except CatalogError as error:
            return _problem(request, error)
        except RecipeLibraryError as error:
            return _recipe_library_problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.managed.sync",
                value.commit,
                (value.id, value.repository, value.commit or ""),
            )
        )
        return _managed_sync(value)

    @app.get(
        "/api/catalog/managed-recipes/sync-status",
        response_model=ManagedCatalogSyncResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            503: {"model": CatalogProblem},
        },
        operation_id="getManagedRecipeCatalogSyncStatus",
    )
    def get_managed_recipe_catalog_sync_status(
        request: Request, actor: Actor = authenticated
    ):
        administrator(actor)
        value = sync_service().latest()
        if value is None:
            return _catalog_problem(
                request,
                status_code=404,
                code="catalog.sync_not_found",
                detail="no managed recipe catalog sync has run yet",
            )
        return _managed_sync(value)


__all__ = ["CATALOG_OPERATION_IDS", "CatalogProblem", "install_catalog_routes"]
