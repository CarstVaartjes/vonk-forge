"""Strict authenticated HTTP surface for the local database recipe catalog."""

from __future__ import annotations

import io
import json
import tempfile
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from .audit import AuditRecord
from .auth import Actor
from .catalog_service import (
    CatalogConflict,
    CatalogError,
    CatalogService,
    CatalogValidationError,
    RecipeDraftInput,
    RecipeRevisionView,
    RecipeSummary,
    _document_summary,
)
from .global_catalog import GlobalCatalogError, GlobalRecipeRevision
from .models import CatalogEntityRevision

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
_MAX_DOCUMENT_BYTES = 256 * 1024

CATALOG_OPERATION_IDS = {
    ("get", "/api/v1/catalog/entities"): "listCatalogEntities",
    ("post", "/api/v1/catalog/entities"): "createCatalogEntityDraft",
    ("get", "/api/v1/catalog/entities/{entity_id}"): "getCatalogEntity",
    (
        "put",
        "/api/v1/catalog/entities/{entity_id}/draft",
    ): "reviseCatalogEntity",
    (
        "post",
        "/api/v1/catalog/entities/{entity_id}/resolve",
    ): "resolveCatalogEntity",
    ("get", "/api/v1/catalog/recipes"): "listLocalRecipes",
    ("post", "/api/v1/catalog/recipes"): "createLocalRecipe",
    ("get", "/api/v1/catalog/recipes/{recipe_id}"): "getLocalRecipe",
    ("put", "/api/v1/catalog/recipes/{recipe_id}/draft"): "updateLocalRecipeDraft",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/resolve"): "resolveLocalRecipe",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/fork"): "forkLocalRecipe",
    (
        "get",
        "/api/v1/catalog/source-bundles/{sha256}",
    ): "downloadLocalRecipeSourceBundle",
    (
        "put",
        "/api/v1/catalog/source-bundles/{sha256}",
    ): "uploadLocalRecipeSourceBundle",
    ("post", "/api/v1/catalog/imports/global/preview"): "previewGlobalRecipeImport",
    ("post", "/api/v1/catalog/imports/global"): "importGlobalRecipe",
    (
        "put",
        "/api/v1/catalog/recipes/{recipe_id}/publication-report",
    ): "attachRecipePublicationReport",
    (
        "post",
        "/api/v1/catalog/recipes/{recipe_id}/publication-export",
    ): "exportRecipeForPublication",
}


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class GlobalCatalogReader(Protocol):
    def fetch(self, uri: str) -> GlobalRecipeRevision: ...
    def fetch_source_bundle(self, sha256: str) -> bytes: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogProblem(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    request_id: str = Field(pattern=_UUID)


class CreateRecipeRequest(StrictModel):
    slug: str = Field(pattern=_SLUG)
    document: dict[str, object]


class UpdateRecipeDraftRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)
    document: dict[str, object]


class ResolveRecipeRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)


class CreateCatalogEntityRequest(StrictModel):
    document: dict[str, object]


class ReviseCatalogEntityRequest(CreateCatalogEntityRequest):
    expected_revision: int = Field(ge=1, strict=True)


class ResolveCatalogEntityRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)


class ForkRecipeRequest(StrictModel):
    revision: int = Field(ge=1, strict=True)
    slug: str = Field(pattern=_SLUG)


class GlobalImportPreviewRequest(StrictModel):
    uri: str = Field(min_length=100, max_length=256)


class GlobalImportRequest(GlobalImportPreviewRequest):
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TestReportRequest(StrictModel):
    report: dict[str, object]


class PublicationExportRequest(StrictModel):
    publisher: str = Field(pattern=_SLUG)


class GlobalRevisionResponse(StrictModel):
    publisher: str = Field(pattern=_SLUG)
    slug: str = Field(pattern=_SLUG)
    recipe_id: str = Field(min_length=32, max_length=36)
    revision_number: int = Field(ge=1)
    revision_id: str = Field(min_length=32, max_length=36)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: str
    document: dict[str, object]


class RecipeSummaryResponse(StrictModel):
    recipe_id: str = Field(pattern=_UUID)
    slug: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=120)
    origin: Literal["local", "workload_run", "global"]
    revision_number: int = Field(ge=1)
    lifecycle: Literal["draft", "blocked", "resolved", "deprecated"]
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_harness: str = Field(pattern=_SLUG)
    runtime_distribution: str = Field(pattern=_SLUG)
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_count: int = Field(ge=0, le=32)
    expected_download_bytes: int = Field(ge=1)
    topology_name: str = Field(min_length=1, max_length=64)
    topology_mode: str = Field(min_length=1, max_length=32)
    node_count: int = Field(ge=1)
    maximum_installed_bytes_per_node: int = Field(ge=1)
    maximum_runtime_memory_bytes_per_node: int = Field(ge=1)


class RecipeListResponse(StrictModel):
    recipes: list[RecipeSummaryResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=64)


class RecipeRevisionResponse(RecipeSummaryResponse):
    id: str = Field(pattern=_UUID)
    description: str = Field(min_length=1, max_length=4000)
    schema_version: Literal[1]
    document: dict[str, object]
    created_by: str = Field(min_length=1, max_length=200)
    created_at: str


class SourceBundleResponse(StrictModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_bytes: int = Field(ge=1)
    total_bytes: int = Field(ge=0)
    file_count: int = Field(ge=1, le=4096)
    files: list[str] = Field(min_length=1, max_length=4096)


class CatalogEntityRevisionResponse(StrictModel):
    entity_id: str = Field(pattern=_UUID)
    kind: Literal[
        "model-group",
        "model",
        "model-version",
        "execution-harness",
        "runtime-distribution",
        "patch-bundle",
    ]
    publisher: str = Field(pattern=_SLUG)
    slug: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=120)
    revision_id: str = Field(pattern=_UUID)
    revision_number: int = Field(ge=1)
    lifecycle: Literal["draft", "blocked", "resolved", "deprecated"]
    schema_version: Literal[1]
    document: dict[str, object]
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_by: str = Field(min_length=1, max_length=200)
    created_at: str


class CatalogEntityListResponse(StrictModel):
    entities: list[CatalogEntityRevisionResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


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


def _summary(value: RecipeSummary) -> dict[str, object]:
    return {
        "recipe_id": value.recipe_id,
        "slug": value.slug,
        "title": value.title,
        "origin": value.source_kind,
        "revision_number": value.revision_number,
        "lifecycle": value.lifecycle,
        "content_sha256": value.content_sha256,
        "execution_harness": value.execution_harness,
        "runtime_distribution": value.runtime_distribution,
        "source_bundle_sha256": value.source_bundle_sha256,
        "artifact_count": value.artifact_count,
        "expected_download_bytes": value.expected_download_bytes,
        "topology_name": value.topology_name,
        "topology_mode": value.topology_mode,
        "node_count": value.node_count,
        "maximum_installed_bytes_per_node": value.maximum_installed_bytes_per_node,
        "maximum_runtime_memory_bytes_per_node": value.maximum_runtime_memory_bytes_per_node,
    }


def _revision(value: RecipeRevisionView) -> dict[str, object]:
    summary = {
        "recipe_id": value.recipe_id,
        "slug": value.slug,
        "title": value.title,
        "origin": value.source_kind,
        "revision_number": value.revision_number,
        "lifecycle": value.lifecycle,
        "content_sha256": value.content_sha256,
        **_document_summary(value.document),
    }
    return {
        **summary,
        "id": value.id,
        "description": value.description,
        "schema_version": value.schema_version,
        "document": value.document,
        "created_by": value.created_by,
        "created_at": value.created_at.isoformat(),
    }


def _entity_revision(value: CatalogEntityRevision) -> dict[str, object]:
    return {
        "entity_id": value.entity_id,
        "kind": value.entity.kind,
        "publisher": value.entity.publisher,
        "slug": value.entity.slug,
        "title": value.entity.title,
        "revision_id": value.id,
        "revision_number": value.revision_number,
        "lifecycle": value.lifecycle,
        "schema_version": value.schema_version,
        "document": value.document,
        "content_sha256": value.content_sha256,
        "created_by": value.created_by,
        "created_at": value.created_at.isoformat(),
    }


def _bounded(
    document: Mapping[str, object], *, subject: str = "recipe document"
) -> None:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise CatalogError("catalog.document_too_large", f"{subject} exceeds 256 KiB")


def install_catalog_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    service: CatalogService | None,
    global_catalog: GlobalCatalogReader | None = None,
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

    def entity_administrator(request: Request, actor: Actor) -> JSONResponse | None:
        if actor.role == "administrator":
            return None
        return _catalog_problem(
            request,
            status_code=403,
            code="catalog.entity_forbidden",
            detail="administrator role is required for catalog entity authoring",
        )

    def entity_not_found(request: Request) -> JSONResponse:
        return _catalog_problem(
            request,
            status_code=404,
            code="catalog.entity_not_found",
            detail="catalog entity or revision was not found",
        )

    def remote(uri: str) -> GlobalRecipeRevision:
        if global_catalog is None:
            raise GlobalCatalogError(
                "global.unavailable", "global catalog is not configured"
            )
        return global_catalog.fetch(uri)

    @app.get(
        "/api/v1/catalog/entities",
        response_model=CatalogEntityListResponse,
        responses={401: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        operation_id="listCatalogEntities",
    )
    def list_entities(
        request: Request,
        kind: str | None = Query(default=None, max_length=32),
        publisher: str | None = Query(default=None, pattern=_SLUG),
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=512),
        _actor: Actor = authenticated,
    ):
        try:
            values, next_cursor = catalog().entities.list_entities(
                kind=kind,
                publisher=publisher,
                limit=limit,
                cursor=cursor,
            )
        except (CatalogError, ValueError) as error:
            problem = (
                error
                if isinstance(error, CatalogError)
                else CatalogValidationError("catalog.kind", "catalog kind is invalid")
            )
            return _problem(request, problem)
        return {
            "entities": [_entity_revision(value) for value in values],
            "next_cursor": next_cursor,
        }

    @app.post(
        "/api/v1/catalog/entities",
        response_model=CatalogEntityRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        status_code=status.HTTP_201_CREATED,
        operation_id="createCatalogEntityDraft",
    )
    def create_entity(
        body: CreateCatalogEntityRequest,
        request: Request,
        actor: Actor = authenticated,
    ):
        if denial := entity_administrator(request, actor):
            return denial
        try:
            _bounded(body.document, subject="catalog entity document")
            result = catalog().entities.create_draft(body.document, actor=actor.subject)
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.entity.create",
                None,
                (result.entity_id, result.id),
            )
        )
        return _entity_revision(result)

    @app.get(
        "/api/v1/catalog/entities/{entity_id}",
        response_model=CatalogEntityRevisionResponse,
        responses={401: {"model": CatalogProblem}, 404: {"model": CatalogProblem}},
        operation_id="getCatalogEntity",
    )
    def get_entity(
        request: Request,
        entity_id: str = Path(pattern=_UUID),
        _actor: Actor = authenticated,
    ):
        try:
            return _entity_revision(catalog().entities.get_entity(entity_id))
        except KeyError:
            return entity_not_found(request)

    @app.put(
        "/api/v1/catalog/entities/{entity_id}/draft",
        response_model=CatalogEntityRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        operation_id="reviseCatalogEntity",
    )
    def revise_entity(
        body: ReviseCatalogEntityRequest,
        request: Request,
        entity_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        if denial := entity_administrator(request, actor):
            return denial
        try:
            _bounded(body.document, subject="catalog entity document")
            result = catalog().entities.revise(
                entity_id,
                body.document,
                actor=actor.subject,
                expected_revision=body.expected_revision,
            )
        except KeyError:
            return entity_not_found(request)
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.entity.revise",
                None,
                (entity_id, result.id),
            )
        )
        return _entity_revision(result)

    @app.post(
        "/api/v1/catalog/entities/{entity_id}/resolve",
        response_model=CatalogEntityRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        operation_id="resolveCatalogEntity",
    )
    def resolve_entity(
        body: ResolveCatalogEntityRequest,
        request: Request,
        entity_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        if denial := entity_administrator(request, actor):
            return denial
        try:
            result = catalog().entities.resolve(
                entity_id,
                actor=actor.subject,
                expected_revision=body.expected_revision,
            )
        except KeyError:
            return entity_not_found(request)
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.entity.resolve",
                None,
                (entity_id, result.id, result.content_sha256 or ""),
            )
        )
        return _entity_revision(result)

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
        operation_id="downloadLocalRecipeSourceBundle",
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
        operation_id="uploadLocalRecipeSourceBundle",
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

    def global_problem(request: Request, error: GlobalCatalogError) -> JSONResponse:
        status_code = (
            404
            if error.code == "global.not_found"
            else 409
            if error.code == "global.revision_changed"
            else 422
            if error.code
            in {
                "global.uri_invalid",
                "global.identity_mismatch",
                "global.schema_incompatible",
            }
            else 503
            if error.code in {"global.unavailable", "global.url_insecure"}
            else 502
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "code": error.code[:128],
                "detail": error.detail[:256],
                "request_id": request.state.request_id,
            },
        )

    def global_revision(value: GlobalRecipeRevision) -> dict[str, object]:
        return {
            "publisher": value.publisher,
            "slug": value.slug,
            "recipe_id": value.recipe_id,
            "revision_number": value.revision_number,
            "revision_id": value.revision_id,
            "content_sha256": value.content_sha256,
            "published_at": value.published_at,
            "document": value.document,
        }

    def read(call, request: Request):
        try:
            return call()
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)

    @app.get(
        "/api/v1/catalog/recipes",
        response_model=RecipeListResponse,
        responses={401: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        operation_id="listLocalRecipes",
    )
    def list_recipes(
        request: Request,
        cursor: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
        _actor: Actor = authenticated,
    ):
        result = read(
            lambda: catalog().list_recipes(limit=limit, cursor=cursor), request
        )
        if isinstance(result, JSONResponse):
            return result
        recipes, next_cursor = result
        return {
            "recipes": [_summary(item) for item in recipes],
            "next_cursor": next_cursor,
        }

    @app.post(
        "/api/v1/catalog/recipes",
        response_model=RecipeRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        status_code=status.HTTP_201_CREATED,
        operation_id="createLocalRecipe",
    )
    def create_recipe(
        body: CreateRecipeRequest, request: Request, actor: Actor = authenticated
    ):
        administrator(actor)
        try:
            _bounded(body.document)
            result = catalog().create_recipe(
                actor.subject, RecipeDraftInput(slug=body.slug, document=body.document)
            )
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.recipe.create",
                None,
                (result.recipe_id,),
            )
        )
        return _revision(result)

    @app.get(
        "/api/v1/catalog/recipes/{recipe_id}",
        response_model=RecipeRevisionResponse,
        responses={401: {"model": CatalogProblem}, 404: {"model": CatalogProblem}},
        operation_id="getLocalRecipe",
    )
    def get_recipe(
        request: Request,
        recipe_id: str = Path(pattern=_UUID),
        _actor: Actor = authenticated,
    ):
        result = read(lambda: catalog().get_recipe(recipe_id), request)
        return result if isinstance(result, JSONResponse) else _revision(result)

    @app.put(
        "/api/v1/catalog/recipes/{recipe_id}/draft",
        response_model=RecipeRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        operation_id="updateLocalRecipeDraft",
    )
    def update_draft(
        body: UpdateRecipeDraftRequest,
        request: Request,
        recipe_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            _bounded(body.document)
            result = catalog().update_draft(
                recipe_id, body.expected_revision, body.document, actor.subject
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.recipe.update",
                None,
                (recipe_id, str(result.revision_number)),
            )
        )
        return _revision(result)

    @app.post(
        "/api/v1/catalog/recipes/{recipe_id}/resolve",
        response_model=RecipeRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        operation_id="resolveLocalRecipe",
    )
    def resolve_recipe(
        body: ResolveRecipeRequest,
        request: Request,
        recipe_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            result = catalog().resolve(recipe_id, body.expected_revision, actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.recipe.resolve",
                None,
                (recipe_id, result.content_sha256 or ""),
            )
        )
        return _revision(result)

    @app.post(
        "/api/v1/catalog/recipes/{recipe_id}/fork",
        response_model=RecipeRevisionResponse,
        responses={
            401: {"model": CatalogProblem},
            403: {"model": CatalogProblem},
            404: {"model": CatalogProblem},
            409: {"model": CatalogProblem},
            422: {"model": CatalogProblem},
        },
        status_code=status.HTTP_201_CREATED,
        operation_id="forkLocalRecipe",
    )
    def fork_recipe(
        body: ForkRecipeRequest,
        request: Request,
        recipe_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            result = catalog().fork(recipe_id, body.revision, body.slug, actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.recipe.fork",
                None,
                (recipe_id, result.recipe_id),
            )
        )
        return _revision(result)

    @app.post(
        "/api/v1/catalog/imports/global/preview",
        response_model=GlobalRevisionResponse,
        operation_id="previewGlobalRecipeImport",
    )
    def preview_global_import(
        body: GlobalImportPreviewRequest, request: Request, actor: Actor = authenticated
    ):
        administrator(actor)
        try:
            return global_revision(remote(body.uri))
        except GlobalCatalogError as error:
            return global_problem(request, error)

    @app.post(
        "/api/v1/catalog/imports/global",
        response_model=RecipeRevisionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="importGlobalRecipe",
    )
    def import_global_recipe(
        body: GlobalImportRequest, request: Request, actor: Actor = authenticated
    ):
        administrator(actor)
        try:
            fetched = remote(body.uri)
        except GlobalCatalogError as error:
            return global_problem(request, error)
        if fetched.content_sha256 != body.expected_content_sha256:
            return _problem(
                request,
                CatalogConflict(
                    "global.preview_changed",
                    "global recipe changed since preview; review it again",
                ),
            )
        try:
            build = fetched.document.get("build")
            context = build.get("context") if isinstance(build, dict) else None
            source_sha256 = context.get("sha256") if isinstance(context, dict) else None
            if not isinstance(source_sha256, str):
                raise CatalogValidationError(
                    "global.source_invalid", "global recipe source identity is invalid"
                )
            source = (
                global_catalog.fetch_source_bundle(source_sha256)
                if global_catalog is not None
                else b""
            )
            catalog().store_source_bundle(
                source_sha256, io.BytesIO(source), actor.subject
            )
            result = catalog().import_global(actor.subject, fetched)
        except CatalogError as error:
            return _problem(request, error)
        except GlobalCatalogError as error:
            return global_problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.global.import",
                None,
                (result.recipe_id, fetched.revision_id, fetched.content_sha256),
            )
        )
        return _revision(result)

    @app.put(
        "/api/v1/catalog/recipes/{recipe_id}/publication-report",
        operation_id="attachRecipePublicationReport",
    )
    def attach_publication_report(
        body: TestReportRequest,
        request: Request,
        recipe_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            _bounded(body.report)
            report = catalog().attach_test_report(recipe_id, body.report, actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.test_report.attach",
                None,
                (recipe_id,),
            )
        )
        return {"report": report}

    @app.post(
        "/api/v1/catalog/recipes/{recipe_id}/publication-export",
        operation_id="exportRecipeForPublication",
    )
    def export_for_publication(
        body: PublicationExportRequest,
        request: Request,
        recipe_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            envelope = catalog().publication_export(recipe_id, body.publisher)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        recipe = envelope["recipe"]
        assert isinstance(recipe, dict)
        identity = recipe["identity"]
        assert isinstance(identity, dict)
        filename = f"{body.publisher}-{identity['slug']}.json"
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.publication.export",
                None,
                (recipe_id, body.publisher),
            )
        )
        return JSONResponse(
            content=envelope,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )


__all__ = ["CATALOG_OPERATION_IDS", "CatalogProblem", "install_catalog_routes"]
