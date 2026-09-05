"""Strict authenticated HTTP surface for the local database recipe catalog."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import uuid
from collections.abc import Mapping
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

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
    RecipeCatalogLocalRevision,
    RecipeDraftInput,
    RecipeRevisionView,
    RecipeSummary,
    _document_summary,
)
from .catalog_sync import CatalogSyncError, CatalogSyncView
from .global_catalog import GlobalCatalogError, GlobalRecipeRevision
from .models import CatalogDocumentRevision
from .recipe_library import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibraryRelease,
    RecipeLibrarySnapshot,
    recipe_release_is_older,
)

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
_NAME = r"^[a-z][a-z0-9_-]{0,63}$"
_SEMVER = (
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MAX_DOCUMENT_BYTES = 256 * 1024
_SOURCE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_PUBLIC_CAPABILITIES = (
    "chat",
    "reasoning",
    "vision",
    "image-generation",
    "image-editing",
    "video",
    "audio",
    "3d",
)
PublicRecipeCapability = Literal[
    "chat",
    "reasoning",
    "vision",
    "image-generation",
    "image-editing",
    "video",
    "audio",
    "3d",
]
PublicRecipeQualification = Literal["candidate", "cataloged"]
PublicRecipeAlignment = Literal[
    "standard", "abliterated", "derisked", "other-modified", "unspecified"
]
PublicRecipeExecutionReadiness = Literal[
    "executable", "not-executable", "integration-required", "not-declared"
]
PublicRecipeExecutionReadinessBasis = Literal[
    "explicit-executable-metadata",
    "explicit-non-executable-metadata",
    "explicit-integration-required-metadata",
    "missing-readiness-metadata",
    "conflicting-readiness-metadata",
]
PublicRecipeQualificationBasis = Literal[
    "explicit-accepted-metadata",
    "explicit-candidate-metadata",
    "missing-accepted-metadata",
    "conflicting-metadata",
]

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
    ("post", "/api/v1/catalog/imports/recipe-library"): "importRecipeLibrary",
    (
        "post",
        "/api/v1/catalog/managed-recipes/sync",
    ): "syncManagedRecipeCatalog",
    (
        "get",
        "/api/v1/catalog/managed-recipes/sync-status",
    ): "getManagedRecipeCatalogSyncStatus",
    ("get", "/api/v1/catalog/public-recipes"): "listPublicRecipes",
    ("post", "/api/v1/catalog/imports/public/preview"): "previewPublicRecipeImport",
    ("post", "/api/v1/catalog/imports/public"): "importPublicRecipe",
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


class RecipeLibraryReader(Protocol):
    def list(self) -> RecipeLibrarySnapshot: ...
    def fetch(self, uri: str) -> RecipeLibraryItem: ...


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


class PublicImportRequest(GlobalImportPreviewRequest):
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecipeLibraryImportRequest(StrictModel):
    library_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_path: str = Field(pattern=r"^recipes/[a-z0-9][a-z0-9-]{1,62}\.json$")
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: dict[str, object]
    model_documents: list[dict[str, object]] = Field(min_length=1, max_length=32)


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


class PublicRecipeChange(StrictModel):
    kind: Literal[
        "initial",
        "model",
        "runtime",
        "performance",
        "fix",
        "security",
        "compatibility",
        "breaking",
        "metadata",
    ]
    summary: str = Field(min_length=1, max_length=160)
    details: str | None = Field(default=None, min_length=1, max_length=1000)
    references: list[str] = Field(max_length=8)


class PublicRecipeRelease(StrictModel):
    version: str = Field(
        pattern=_SEMVER,
        max_length=64,
    )
    released_at: str = Field(min_length=10, max_length=10)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upgrade_effect: Literal["metadata-only", "restart", "reinstall", "rebuild"]
    changes: list[PublicRecipeChange] = Field(min_length=1, max_length=16)


class PublicRecipeLocalState(StrictModel):
    status: Literal[
        "not-imported",
        "current",
        "update-available",
        "local-ahead",
        "different-revision",
        "conflict",
    ]
    recipe_id: str | None = Field(default=None, pattern=_UUID)
    revision_number: int | None = Field(default=None, ge=1)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    release_version: str | None = Field(default=None, pattern=_SEMVER, max_length=64)


class PublicRecipeDiskRequirements(StrictModel):
    image_bytes: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)
    staging_bytes: int = Field(ge=0)
    cache_bytes: int = Field(ge=0)
    rollback_bytes: int = Field(ge=0)
    safety_margin_bytes: int = Field(ge=0)


class PublicRecipeArtifactIdentity(StrictModel):
    artifact_id: str = Field(pattern=_NAME)
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_bytes: int = Field(gt=0)
    installed_bytes: int = Field(gt=0)
    roles: list[str] = Field(max_length=32)


class PublicRecipeTopologyRole(StrictModel):
    name: str = Field(pattern=_NAME)
    count: int = Field(ge=1)
    endpoint_owner: bool
    disk: PublicRecipeDiskRequirements


class PublicRecipeFabric(StrictModel):
    connectivity: Literal["none", "connected", "full_mesh", "switch"]
    minimum_bandwidth_mbps: int = Field(ge=0)


class PublicRecipeListItem(StrictModel):
    publisher: str = Field(pattern=_SLUG)
    slug: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(max_length=32)
    uri: str = Field(min_length=100, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_publisher: str = Field(pattern=_SLUG)
    model_slug: str = Field(pattern=_SLUG)
    model_title: str = Field(min_length=1, max_length=120)
    model_version_publisher: str = Field(pattern=_SLUG)
    model_version_slug: str = Field(pattern=_SLUG)
    model_version_title: str = Field(min_length=1, max_length=120)
    source_owner: str | None = Field(default=None, min_length=1, max_length=120)
    source_repository: str | None = Field(default=None, min_length=1, max_length=512)
    alignment: PublicRecipeAlignment
    capabilities: list[PublicRecipeCapability] = Field(max_length=8)
    qualification: PublicRecipeQualification
    qualification_basis: PublicRecipeQualificationBasis
    qualification_detail: str = Field(min_length=1, max_length=256)
    execution_readiness: PublicRecipeExecutionReadiness
    execution_readiness_basis: PublicRecipeExecutionReadinessBasis
    execution_readiness_detail: str = Field(min_length=1, max_length=256)
    precision: str | None = Field(default=None, min_length=2, max_length=24)
    quantizations: list[str] = Field(max_length=16)
    execution_harness: str = Field(pattern=_SLUG)
    runtime_distribution: str = Field(pattern=_SLUG)
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_count: int = Field(ge=0, le=32)
    artifact_identities: list[PublicRecipeArtifactIdentity] = Field(max_length=32)
    topology_name: str = Field(min_length=1, max_length=64)
    topology_mode: str = Field(min_length=1, max_length=32)
    node_count: int = Field(ge=1)
    topology_roles: list[PublicRecipeTopologyRole] = Field(min_length=1, max_length=32)
    fabric: PublicRecipeFabric
    expected_download_bytes: int = Field(ge=1)
    maximum_installed_bytes_per_node: int = Field(ge=1)
    temporary_build_bytes_per_node: int = Field(ge=0)
    maximum_runtime_memory_bytes_per_node: int = Field(ge=1)
    release_version: str | None = Field(default=None, pattern=_SEMVER, max_length=64)
    release_released_at: str | None = Field(default=None, min_length=10, max_length=10)
    local: PublicRecipeLocalState


class PublicRecipeListResponse(StrictModel):
    repository: str = Field(min_length=1, max_length=200)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    recipes: list[PublicRecipeListItem] = Field(max_length=256)


class PublicRecipePreviewResponse(PublicRecipeListItem):
    source: Literal["global", "recipe_library"]
    changes_since_local: list[PublicRecipeRelease] = Field(max_length=32)


class RecipeSummaryResponse(StrictModel):
    recipe_id: str = Field(pattern=_UUID)
    slug: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=120)
    origin: Literal["local", "workload_run", "global", "recipe_library"]
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
    schema_version: Literal[2]
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
    kind: Literal["model", "recipe"]
    publisher: str = Field(pattern=_SLUG)
    slug: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=120)
    revision_id: str = Field(pattern=_UUID)
    revision_number: int = Field(ge=1)
    lifecycle: Literal["candidate", "active", "failed"]
    schema_version: Literal[2]
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


def _entity_revision(value: CatalogDocumentRevision) -> dict[str, object]:
    metadata = value.document.get("metadata", {})
    title = metadata.get("title") if isinstance(metadata, Mapping) else None
    if not isinstance(title, str):
        identity = value.document.get("identity", {})
        title = identity.get("slug", value.slug) if isinstance(identity, Mapping) else value.slug
    return {
        "entity_id": value.entity_id,
        "kind": value.kind,
        "publisher": value.publisher,
        "slug": value.slug,
        "title": title,
        "revision_id": value.id,
        "revision_number": value.revision_number,
        "lifecycle": value.state,
        "schema_version": value.schema_version,
        "document": value.document,
        "content_sha256": value.content_digest,
        "created_by": value.created_by,
        "created_at": value.created_at.isoformat(),
    }


def _public_recipe_metadata(
    document: Mapping[str, object],
    dependencies: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    selections = document.get("models")
    first_selection = selections[0] if isinstance(selections, list) and selections else {}
    model_version = first_selection.get("model") if isinstance(first_selection, Mapping) else {}
    model_version = model_version if isinstance(model_version, Mapping) else {}
    model_version_publisher = str(model_version.get("publisher", "unknown"))
    model_version_slug = str(model_version.get("slug", "unknown"))
    model_version_title = model_version_slug
    model_publisher = model_version_publisher
    model_slug = model_version_slug
    model_title = model_slug
    model_reference: Mapping[str, object] | None = None
    for dependency in dependencies:
        identity = dependency.get("identity")
        if (
            dependency.get("kind") == "model"
            and isinstance(identity, Mapping)
            and identity.get("publisher") == model_version_publisher
            and identity.get("slug") == model_version_slug
        ):
            model_reference = identity
            metadata = dependency.get("metadata")
            if isinstance(metadata, Mapping):
                model_version_title = str(metadata.get("title", model_version_slug)) or model_version_slug
            break
    if model_reference is not None:
        referenced_publisher = model_reference.get("publisher")
        referenced_slug = model_reference.get("slug")
        if isinstance(referenced_publisher, str) and isinstance(referenced_slug, str):
            model_publisher = referenced_publisher
            model_slug = referenced_slug
            model_title = model_slug
            for dependency in dependencies:
                identity = dependency.get("identity")
                if (
                    dependency.get("kind") == "model"
                    and isinstance(identity, Mapping)
                    and identity.get("publisher") == model_publisher
                    and identity.get("slug") == model_slug
                ):
                    metadata = dependency.get("metadata")
                    if isinstance(metadata, Mapping):
                        model_title = str(metadata.get("title", model_slug))
                    break

    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    tags = _public_recipe_tags(document)
    raw_interfaces = document.get("interfaces", [])
    raw_interfaces = raw_interfaces if isinstance(raw_interfaces, list) else []
    adapters = {
        str(interface.get("adapter", ""))
        for interface in raw_interfaces
        if isinstance(interface, Mapping)
    }
    capabilities: set[str] = set()
    if "openai" in adapters:
        capabilities.add("chat")
    if "reasoning" in tags:
        capabilities.add("reasoning")
    if tags.intersection({"vision", "multimodal", "omni"}):
        capabilities.add("vision")
    editing = bool(tags.intersection({"editing", "image-to-image", "layered"}))
    if editing:
        capabilities.add("image-editing")
    if ("image-job" in adapters and not editing) or tags.intersection(
        {"generation", "text-to-image"}
    ):
        capabilities.add("image-generation")
    if "video-job" in adapters or "video" in tags:
        capabilities.add("video")
    if "audio-job" in adapters or "audio" in tags:
        capabilities.add("audio")
    if "mesh-job" in adapters or tags.intersection({"three-d", "3d", "mesh"}):
        capabilities.add("3d")

    title = str(metadata.get("title", ""))
    precision_tokens = set(tags)
    precision_tokens.update(re.findall(r"[a-z0-9]+", f"{title} {model_version_title}".lower()))
    quantizations = [
        value.upper() if value != "torchao" else "TorchAO"
        for value in (
            "nvfp4",
            "bf16",
            "fp8",
            "fp4",
            "fp16",
            "int8",
            "int4",
            "exl3",
            "aqlm",
            "awq",
            "gptq",
            "gguf",
            "torchao",
        )
        if value in precision_tokens
    ]
    precision = quantizations[0] if quantizations else None
    source_owner, source_repository = _public_recipe_source(document)
    alignment = _public_recipe_alignment(document)
    qualification, qualification_basis, qualification_detail = (
        _public_recipe_qualification(tags)
    )
    execution_readiness, execution_readiness_basis, execution_readiness_detail = (
        _public_recipe_execution_readiness(tags)
    )
    topology = document.get("topology")
    topology = topology if isinstance(topology, Mapping) else {}
    raw_roles = topology.get("roles")
    raw_roles = raw_roles if isinstance(raw_roles, list) else []
    topology_roles = []
    for role in raw_roles:
        if not isinstance(role, Mapping):
            continue
        resources = role.get("resources")
        disk = resources.get("disk") if isinstance(resources, Mapping) else None
        topology_roles.append(
            {
                "name": str(role.get("name", "")),
                "count": int(role.get("count", 0)),
                "endpoint_owner": bool(role.get("endpoint_owner", False)),
                "disk": (
                    {
                        field: disk.get(field)
                        for field in (
                            "image_bytes",
                            "artifact_bytes",
                            "staging_bytes",
                            "cache_bytes",
                            "rollback_bytes",
                            "safety_margin_bytes",
                        )
                    }
                    if isinstance(disk, Mapping)
                    else None
                ),
            }
        )
    artifact_identities = []
    for model in dependencies:
        if model.get("kind") != "model":
            continue
        identity = model.get("identity")
        files = model.get("files")
        if not isinstance(identity, Mapping) or not isinstance(files, list):
            continue
        for model_file in files:
            if not isinstance(model_file, Mapping):
                continue
            file_identity = {
                "publisher": identity.get("publisher"),
                "slug": identity.get("slug"),
                "id": model_file.get("id"),
                "path": model_file.get("path"),
                "sha256": model_file.get("sha256"),
                "size_bytes": model_file.get("size_bytes"),
            }
            artifact_identities.append(
                {
                    "artifact_id": model_file.get("id"),
                    "identity_sha256": hashlib.sha256(
                        json.dumps(
                            file_identity, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    "download_bytes": model_file.get("size_bytes"),
                    "installed_bytes": model_file.get("size_bytes"),
                    "roles": sorted(
                        set(model_file.get("roles", []))
                    ),
                }
            )
    artifact_identities.sort(key=lambda item: str(item["identity_sha256"]))
    execution = document.get("execution")
    build = execution.get("build") if isinstance(execution, Mapping) else None
    build_resources = build.get("resources") if isinstance(build, Mapping) else None
    raw_fabric = topology.get("fabric")
    raw_fabric = raw_fabric if isinstance(raw_fabric, Mapping) else {}
    return {
        "model_publisher": model_publisher,
        "model_slug": model_slug,
        "model_title": model_title,
        "model_version_publisher": model_version_publisher,
        "model_version_slug": model_version_slug,
        "model_version_title": model_version_title,
        "source_owner": source_owner,
        "source_repository": source_repository,
        "alignment": alignment,
        "capabilities": [
            capability
            for capability in _PUBLIC_CAPABILITIES
            if capability in capabilities
        ],
        "qualification": qualification,
        "qualification_basis": qualification_basis,
        "qualification_detail": qualification_detail,
        "execution_readiness": execution_readiness,
        "execution_readiness_basis": execution_readiness_basis,
        "execution_readiness_detail": execution_readiness_detail,
        "precision": precision,
        "quantizations": quantizations,
        "topology_roles": topology_roles,
        "artifact_identities": artifact_identities,
        "temporary_build_bytes_per_node": (
            build_resources.get("temporary_bytes")
            if isinstance(build_resources, Mapping)
            else None
        ),
        "fabric": {
            "connectivity": raw_fabric.get("connectivity", "none"),
            "minimum_bandwidth_mbps": raw_fabric.get(
                "minimum_bandwidth_mbps", 0
            ),
        },
        **_document_summary(document),
    }


def _public_recipe_qualification(
    tags: set[str],
) -> tuple[
    PublicRecipeQualification,
    PublicRecipeQualificationBasis,
    str,
]:
    """Project explicit immutable recipe qualification, failing closed.

    ``cataloged`` remains the accepted response literal for client compatibility.
    The reviewed recipe document must now explicitly carry the ``accepted`` tag;
    an absent or contradictory declaration cannot silently grant acceptance.
    """

    accepted = "accepted" in tags
    candidate = "candidate" in tags
    if accepted and not candidate:
        return (
            "cataloged",
            "explicit-accepted-metadata",
            "The reviewed immutable recipe explicitly declares accepted qualification.",
        )
    if accepted and candidate:
        return (
            "candidate",
            "conflicting-metadata",
            "Conflicting accepted and candidate declarations fail closed to Candidate.",
        )
    if candidate:
        return (
            "candidate",
            "explicit-candidate-metadata",
            "The reviewed immutable recipe explicitly remains a Candidate.",
        )
    return (
        "candidate",
        "missing-accepted-metadata",
        "No explicit accepted qualification is attached to this immutable recipe.",
    )


def _public_recipe_execution_readiness(
    tags: set[str],
) -> tuple[
    PublicRecipeExecutionReadiness,
    PublicRecipeExecutionReadinessBasis,
    str,
]:
    """Keep runtime completeness independent from review qualification."""

    executable = "executable" in tags
    non_executable = bool(tags.intersection({"non-executable", "metadata-only"}))
    integration_required = "integration-required" in tags
    declarations = sum((executable, non_executable, integration_required))
    if declarations > 1:
        return (
            "not-executable" if non_executable else "integration-required",
            "conflicting-readiness-metadata",
            "Conflicting execution-readiness declarations fail closed to the most restrictive state.",
        )
    if executable:
        return (
            "executable",
            "explicit-executable-metadata",
            "This recipe explicitly declares a complete executable runtime contract; fleet compatibility and operator review still apply.",
        )
    if non_executable:
        return (
            "not-executable",
            "explicit-non-executable-metadata",
            "This recipe explicitly declares metadata-only or non-executable content.",
        )
    if integration_required:
        return (
            "integration-required",
            "explicit-integration-required-metadata",
            "This recipe requires runtime integration before it can be executed.",
        )
    return (
        "not-declared",
        "missing-readiness-metadata",
        "The immutable recipe does not explicitly declare execution readiness.",
    )


def _public_recipe_tags(document: Mapping[str, object]) -> set[str]:
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    raw_tags = metadata.get("tags", [])
    raw_tags = raw_tags if isinstance(raw_tags, list) else []
    return {value.lower() for value in raw_tags if isinstance(value, str)}


_PUBLIC_RECIPE_ALIGNMENTS = frozenset(
    {"standard", "abliterated", "derisked", "other-modified", "unspecified"}
)


def _public_recipe_alignment(document: Mapping[str, object]) -> str:
    """Return recipe-owned alignment only; model lineage must not imply recipe behavior."""

    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    raw = metadata.get("alignment")
    return raw if isinstance(raw, str) and raw in _PUBLIC_RECIPE_ALIGNMENTS else "unspecified"


def _public_recipe_source(
    document: Mapping[str, object],
) -> tuple[str | None, str | None]:
    provenance = document.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    source_reference = provenance.get("source_reference")
    if not isinstance(source_reference, str):
        return None, None

    canonical = _canonical_source_repository(source_reference)
    if canonical is None:
        return None, None
    return canonical


def _public_release(release: RecipeLibraryRelease) -> dict[str, object]:
    return {
        "version": release.version,
        "released_at": release.released_at,
        "content_sha256": release.content_sha256,
        "upgrade_effect": release.upgrade_effect,
        "changes": [
            {
                "kind": change.kind,
                "summary": change.summary,
                "details": change.details,
                "references": list(change.references),
            }
            for change in release.changes
        ],
    }


def _public_recipe_release_state(
    *,
    source_kind: Literal["global", "recipe_library"],
    publisher: str,
    content_sha256: str,
    releases: tuple[RecipeLibraryRelease, ...],
    local: RecipeCatalogLocalRevision | None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    current = releases[0] if releases else None
    release_version = current.version if current is not None else None
    release_released_at = current.released_at if current is not None else None
    if local is None:
        state: dict[str, object] = {
            "status": "not-imported",
            "recipe_id": None,
            "revision_number": None,
            "content_sha256": None,
            "release_version": None,
        }
        changes = list(reversed(releases))
    else:
        status = "different-revision"
        matched_version = local.release_version
        matched_index: int | None = None
        if local.source_kind != source_kind or local.publisher != publisher:
            status = "conflict"
        elif local.content_sha256 == content_sha256:
            status = "current"
            matched_version = release_version or matched_version
            matched_index = 0
        else:
            for index, release in enumerate(releases):
                if release.content_sha256 == local.content_sha256:
                    matched_version = release.version
                    matched_index = index
                    status = "update-available" if index > 0 else "current"
                    break
            if (
                matched_index is None
                and current is not None
                and local.release_version is not None
                and recipe_release_is_older(current.version, local.release_version)
            ):
                status = "local-ahead"
        state = {
            "status": status,
            "recipe_id": local.recipe_id,
            "revision_number": local.revision_number,
            "content_sha256": local.content_sha256,
            "release_version": matched_version,
        }
        changes = (
            list(reversed(releases[:matched_index]))
            if status == "update-available" and matched_index is not None
            else ([] if status == "current" else list(releases[:1]))
        )
    return (
        {
            "release_version": release_version,
            "release_released_at": release_released_at,
            "local": state,
        },
        [_public_release(release) for release in changes],
    )


def _canonical_source_repository(
    source_reference: str,
) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(source_reference.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
    ):
        return None

    hostname = (parsed.hostname or "").lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or any(
        value in {".", ".."} or not _SOURCE_PATH_SEGMENT.fullmatch(value)
        for value in segments
    ):
        return None

    repository_segments: list[str]
    canonical_segments: list[str]
    if hostname in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
        repository_segments = segments[:2]
        canonical_segments = repository_segments
        hostname = "github.com"
    elif hostname in {"gitlab.com", "www.gitlab.com"}:
        if "-" not in segments and len(segments) != 2:
            return None
        separator = segments.index("-") if "-" in segments else 2
        repository_segments = segments[:separator]
        canonical_segments = repository_segments
        hostname = "gitlab.com"
    elif hostname in {"huggingface.co", "www.huggingface.co"}:
        offset = 1 if segments[0] in {"datasets", "spaces"} else 0
        repository_segments = segments[offset : offset + 2]
        canonical_segments = [*segments[:offset], *repository_segments]
        hostname = "huggingface.co"
    else:
        return None

    if len(repository_segments) < 2:
        return None
    repository_segments[-1] = repository_segments[-1].removesuffix(".git")
    canonical_segments[-1] = repository_segments[-1]
    if not repository_segments[-1]:
        return None
    owner = repository_segments[0]
    repository = urlunsplit(
        ("https", hostname, f"/{'/'.join(canonical_segments)}", "", "")
    )
    if len(owner) > 120 or len(repository) > 512:
        return None
    return owner, repository


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
    recipe_library: RecipeLibraryReader | None = None,
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

    def public_remote(uri: str) -> RecipeLibraryItem:
        if recipe_library is None:
            raise RecipeLibraryError(
                "recipe_library.unavailable",
                "recipe library is not configured",
            )
        return recipe_library.fetch(uri)

    def sync_service() -> ManagedRecipeCatalogSync:
        if managed_sync is None:
            raise HTTPException(
                status_code=503, detail="managed recipe catalog sync is unavailable"
            )
        return managed_sync

    def public_preview(value: RecipeLibraryItem) -> dict[str, object]:
        release_state, changes = _public_recipe_release_state(
            source_kind="recipe_library",
            publisher=value.publisher,
            content_sha256=value.content_sha256,
            releases=value.release_history,
            local=catalog()
            .recipe_catalog_local_revisions([value.slug])
            .get(value.slug),
        )
        return {
            "publisher": value.publisher,
            "slug": value.slug,
            "title": value.title,
            "description": value.description,
            "tags": list(value.tags),
            "uri": value.uri,
            "content_sha256": value.content_sha256,
            "source": "recipe_library",
            **_public_recipe_metadata(value.document, value.dependencies),
            **release_state,
            "changes_since_local": changes,
        }

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

    def recipe_library_problem(
        request: Request, error: RecipeLibraryError
    ) -> JSONResponse:
        status_code = (
            404
            if error.code == "recipe_library.not_found"
            else 409
            if error.code == "recipe_library.digest_mismatch"
            else 422
            if error.code
            in {"recipe_library.uri_invalid", "recipe_library.schema_incompatible"}
            else 503
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

    @app.get(
        "/api/v1/catalog/public-recipes",
        response_model=PublicRecipeListResponse,
        operation_id="listPublicRecipes",
    )
    def list_public_recipes(request: Request, _actor: Actor = authenticated):
        administrator(_actor)
        if recipe_library is None:
            return recipe_library_problem(
                request,
                RecipeLibraryError(
                    "recipe_library.unavailable",
                    "recipe library is not configured",
                ),
            )
        try:
            snapshot = recipe_library.list()
            local_revisions = catalog().recipe_catalog_local_revisions(
                [item.slug for item in snapshot.items]
            )
        except RecipeLibraryError as error:
            return recipe_library_problem(request, error)
        except CatalogError as error:
            return _problem(request, error)
        return {
            "repository": snapshot.repository,
            "commit": snapshot.commit,
            "recipes": [
                {
                    "publisher": item.publisher,
                    "slug": item.slug,
                    "title": item.title,
                    "description": item.description,
                    "tags": list(item.tags),
                    "uri": item.uri,
                    "content_sha256": item.content_sha256,
                    **_public_recipe_metadata(item.document, item.dependencies),
                    **_public_recipe_release_state(
                        source_kind="recipe_library",
                        publisher=item.publisher,
                        content_sha256=item.content_sha256,
                        releases=item.release_history,
                        local=local_revisions.get(item.slug),
                    )[0],
                }
                for item in snapshot.items
            ],
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

    @app.post(
        "/api/v1/catalog/imports/public/preview",
        response_model=PublicRecipePreviewResponse,
        operation_id="previewPublicRecipeImport",
    )
    def preview_public_import(
        body: GlobalImportPreviewRequest,
        request: Request,
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = public_remote(body.uri)
            return public_preview(value)
        except RecipeLibraryError as error:
            return recipe_library_problem(request, error)

    @app.post(
        "/api/v1/catalog/imports/public",
        response_model=RecipeRevisionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="importPublicRecipe",
    )
    def import_public_recipe(
        body: PublicImportRequest,
        request: Request,
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = public_remote(body.uri)
            preview = public_preview(value)
            if preview["content_sha256"] != body.expected_content_sha256:
                return _problem(
                    request,
                    CatalogConflict(
                        "public.preview_changed",
                        "public recipe changed since preview; review it again",
                    ),
                )
            if preview["execution_readiness"] != "executable":
                return _problem(
                    request,
                    CatalogValidationError(
                        "public.recipe_not_executable",
                        "public recipe cannot be imported because it does not "
                        "explicitly declare a complete executable runtime contract",
                    ),
                )
            source_bundle = getattr(value, "source_bundle", None)
            source_sha256 = getattr(value, "source_bundle_sha256", None)
            if source_bundle is not None:
                if not isinstance(source_sha256, str):
                    raise CatalogValidationError(
                        "recipe_library.source_invalid",
                        "recipe library source identity is invalid",
                    )
                catalog().store_source_bundle(
                    source_sha256,
                    io.BytesIO(source_bundle),
                    actor.subject,
                )
            result = catalog().import_recipe_library(
                actor.subject,
                library_commit=value.library_commit,
                source_path=value.source_path,
                document=value.document,
                expected_content_sha256=value.content_sha256,
                dependency_documents=value.dependencies,
                package_handle=getattr(value, "package_handle", None),
                package_sha256=getattr(value, "package_sha256", None),
                source_bundle_sha256=source_sha256,
                release_version=(
                    value.release_history[0].version
                    if value.release_history
                    else None
                ),
                release_released_at=(
                    value.release_history[0].released_at
                    if value.release_history
                    else None
                ),
            )
            action = "catalog.public.import"
        except CatalogError as error:
            return _problem(request, error)
        except RecipeLibraryError as error:
            return recipe_library_problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                action,
                None,
                (result.recipe_id, result.content_sha256 or ""),
            )
        )
        return _revision(result)

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

    @app.post(
        "/api/v1/catalog/imports/recipe-library",
        response_model=RecipeRevisionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="importRecipeLibrary",
    )
    def import_recipe_library(
        body: RecipeLibraryImportRequest,
        request: Request,
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            _bounded(body.document)
            result = catalog().import_recipe_library(
                actor.subject,
                library_commit=body.library_commit,
                source_path=body.source_path,
                document=body.document,
                expected_content_sha256=body.expected_content_sha256,
                dependency_documents=body.model_documents,
            )
        except CatalogError as error:
            return _problem(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.recipe_library.import",
                None,
                (result.recipe_id, result.content_sha256 or ""),
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
