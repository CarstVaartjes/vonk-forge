"""Strict authenticated HTTP surface for the local database recipe catalog."""

from __future__ import annotations

import tempfile
import uuid
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from .audit import AuditRecord
from .auth import Actor
from .catalog_service import (
    CatalogConflict,
    CatalogError,
    CatalogService,
)
from .catalog_sync import CatalogSyncError, CatalogSyncView

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SEMVER = (
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

CATALOG_OPERATION_IDS = {
    (
        "get",
        "/api/v1/catalog/source-bundles/{sha256}",
    ): "downloadRecipeSourceBundle",
    (
        "put",
        "/api/v1/catalog/source-bundles/{sha256}",
    ): "uploadRecipeSourceBundle",
    (
        "post",
        "/api/v1/catalog/managed-recipes/sync",
    ): "syncManagedRecipeCatalog",
    (
        "get",
        "/api/v1/catalog/managed-recipes/sync-status",
    ): "getManagedRecipeCatalogSyncStatus",
}


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogProblem(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    request_id: str = Field(pattern=_UUID)


class ManagedCatalogSyncRequest(StrictModel):
    request_key: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=_UUID)
    expected_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class ManagedCatalogSyncProblem(StrictModel):
    recipe_uri: str | None = Field(default=None, max_length=256)
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)


class ManagedCatalogWithdrawnRecipe(StrictModel):
    recipe_id: str = Field(pattern=_UUID)
    recipe_uri: str | None = Field(default=None, max_length=256)
    release_version: str | None = Field(default=None, pattern=_SEMVER, max_length=64)
    model_version_key: str | None = Field(default=None, max_length=256)


class ManagedCatalogStaleRecipe(StrictModel):
    recipe_id: str = Field(pattern=_UUID)
    current_revision_id: str = Field(pattern=_UUID)
    stale_installation_count: int = Field(ge=0)
    stale_run_count: int = Field(ge=0)


class ManagedCatalogSyncResponse(StrictModel):
    schema_version: Literal[1] = 1
    sync_id: str = Field(pattern=_UUID)
    request_key: str = Field(pattern=_UUID)
    trigger: Literal["manual", "automatic"]
    state: Literal["syncing", "current", "partial", "failed"]
    repository: str = Field(min_length=1, max_length=200)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    total_count: int = Field(ge=0, le=256)
    processed_count: int = Field(ge=0, le=256)
    imported_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    withdrawn_count: int = Field(ge=0)
    withdrawn_recipes: list[ManagedCatalogWithdrawnRecipe] = Field(max_length=256)
    stale_recipes: list[ManagedCatalogStaleRecipe] = Field(max_length=256)
    problems: list[ManagedCatalogSyncProblem] = Field(max_length=256)
    created_at: str
    completed_at: str | None


class SourceBundleResponse(StrictModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_bytes: int = Field(ge=1)
    total_bytes: int = Field(ge=0)
    file_count: int = Field(ge=1, le=4096)
    files: list[str] = Field(min_length=1, max_length=4096)


def _catalog_problem(
    request: Request, *, status_code: int, code: str, detail: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code[:128],
            "detail": detail[:256],
            "request_id": request.state.request_id,
        },
    )


def _problem(request: Request, error: CatalogError) -> JSONResponse:
    status_code = 409 if isinstance(error, CatalogConflict) else 422
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

    @app.get(
        "/api/v1/catalog/source-bundles/{sha256}",
        responses={
            200: {
                "content": {
                    "application/vnd.vonk-forge.source-bundle.v1+tar": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                }
            },
            401: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        operation_id="downloadRecipeSourceBundle",
    )
    def download_source_bundle(
        request: Request,
        sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
        _actor: Actor = authenticated,
    ):
        try:
            archive = catalog().read_source_bundle(sha256)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="source bundle not found"
            ) from None
        except CatalogError as error:
            return _problem(request, error)
        return Response(
            archive,
            media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Disposition": f'attachment; filename="vonk-source-{sha256}.tar"',
                "ETag": f'"sha256:{sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put(
        "/api/v1/catalog/source-bundles/{sha256}",
        response_model=SourceBundleResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        operation_id="uploadRecipeSourceBundle",
    )
    async def upload_source_bundle(
        request: Request,
        sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        maximum = 64 * 1024 * 1024
        received = 0
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as payload:
            async for chunk in request.stream():
                received += len(chunk)
                if received > maximum:
                    return _problem(
                        request,
                        CatalogError(
                            "bundle.archive_too_large",
                            "source bundle is too large",
                        ),
                    )
                payload.write(chunk)
            payload.seek(0)
            try:
                result = catalog().store_source_bundle(sha256, payload, actor.subject)
            except CatalogError as error:
                return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.source_bundle.upload",
                None,
                (sha256, str(result.archive_bytes)),
            )
        )
        return {
            "sha256": result.sha256,
            "archive_bytes": result.archive_bytes,
            "total_bytes": result.total_bytes,
            "file_count": result.file_count,
            "files": list(result.files),
        }

    @app.post(
        "/api/v1/catalog/managed-recipes/sync",
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
        "/api/v1/catalog/managed-recipes/sync-status",
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
