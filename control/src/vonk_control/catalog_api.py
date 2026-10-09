"""Strict authenticated HTTP surface for the local database recipe catalog."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field
from starlette.responses import JSONResponse
from vonk_agent_protocol import CatalogSyncCode

from .auth import Actor
from .catalog_service import (
    CatalogService,
)
from .catalog_sync import CatalogSyncView
from .catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogStaleRecipe,
    ManagedCatalogSyncProblem,
    ManagedCatalogWithdrawnRecipe,
)
from .library_contract import UuidId
from .machine_states import CatalogSyncStateField
from .strict_json import StrictModel

CATALOG_OPERATION_IDS = {
    (
        "get",
        "/api/catalog/managed-recipes/sync-status",
    ): "getManagedRecipeCatalogSyncStatus",
}


class ManagedRecipeCatalogSync(Protocol):
    def latest(self) -> CatalogSyncView | None: ...


class CatalogProblem(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    request_id: UuidId


class ManagedCatalogSyncFailure(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    occurred_at: str


class ManagedCatalogSyncResponse(StrictModel):
    sync_id: UuidId
    request_key: UuidId
    trigger: Literal["manual", "automatic"]
    state: CatalogSyncStateField
    repository: str = Field(min_length=1, max_length=200)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    library_version: str | None = Field(default=None, max_length=32)
    library_updated_at: str | None = None
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


def _managed_sync(value: CatalogSyncView) -> ManagedCatalogSyncResponse:
    return ManagedCatalogSyncResponse(
        sync_id=value.id,
        request_key=value.request_key,
        trigger=CatalogSyncTrigger(value.trigger).value,
        state=value.state,
        repository=value.repository,
        commit=value.commit,
        expected_commit=value.expected_commit,
        library_version=value.library_version,
        library_updated_at=(
            value.library_updated_at.isoformat()
            if value.library_updated_at is not None
            else None
        ),
        total_count=value.total_count,
        processed_count=value.processed_count,
        imported_count=value.imported_count,
        updated_count=value.updated_count,
        unchanged_count=value.unchanged_count,
        skipped_count=value.skipped_count,
        withdrawn_count=value.withdrawn_count,
        withdrawn_recipes=list(value.withdrawn_recipes),
        stale_recipes=list(value.stale_recipes),
        problems=list(value.problems),
        created_at=value.created_at.isoformat(),
        completed_at=(
            value.completed_at.isoformat() if value.completed_at is not None else None
        ),
        last_error=(
            ManagedCatalogSyncFailure(
                code=value.last_error.code,
                detail=value.last_error.detail,
                occurred_at=value.last_error.occurred_at.isoformat(),
            )
            if value.last_error is not None
            else None
        ),
    )


def install_catalog_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: CatalogService | None,
    managed_sync: ManagedRecipeCatalogSync | None = None,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(CATALOG_OPERATION_IDS)
    authenticated = actor_dependency

    def administrator(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def sync_service() -> ManagedRecipeCatalogSync:
        if managed_sync is None:
            raise HTTPException(
                status_code=503, detail="managed recipe catalog sync is unavailable"
            )
        return managed_sync

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
                code=CatalogSyncCode.NOT_FOUND,
                detail="no managed recipe catalog sync has run yet",
            )
        return _managed_sync(value)


__all__ = ["CATALOG_OPERATION_IDS", "CatalogProblem", "install_catalog_routes"]
