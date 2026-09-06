from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.catalog_api import (
    ManagedCatalogSyncProblem,
    ManagedCatalogSyncResponse,
    install_catalog_routes,
)
from vonk_control.catalog_service import CatalogConflict, CatalogError
from vonk_control.catalog_sync import CatalogSyncError
from vonk_control.recipe_library_types import RecipeLibraryError


def _administrator() -> Actor:
    return Actor("test", "administrator")


class _FailingSync:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def sync(self, **_kwargs):
        raise self.error

    def latest(self):
        return None


def _sync_client(error: Exception) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def request_identity(request, call_next):
        request.state.request_id = "00000000-0000-4000-8000-000000000001"
        return await call_next(request)

    install_catalog_routes(
        app,
        actor_dependency=Depends(_administrator),
        audits=MemoryAuditStore(),
        service=None,
        managed_sync=_FailingSync(error),
    )
    return TestClient(app)


def test_catalog_api_exposes_only_canonical_bundle_and_sync_routes() -> None:
    app = FastAPI()
    install_catalog_routes(
        app,
        actor_dependency=_administrator,
        audits=MemoryAuditStore(),
        service=None,
    )
    paths = app.openapi()["paths"]
    assert "/api/v1/catalog/source-bundles/{sha256}" in paths
    assert "/api/v1/catalog/managed-recipes/sync" in paths
    assert "/api/v1/catalog/managed-recipes/sync-status" in paths
    assert "/api/v1/catalog/public-recipes" not in paths
    assert "/api/v1/catalog/imports/public" not in paths
    assert "/api/v1/catalog/imports/recipe-library" not in paths
    assert "/api/v1/catalog/imports/workload_run" not in paths
    assert "/api/v1/catalog/imports/workload_run/preview" not in paths
    assert "/api/v1/catalog/recipes" not in paths
    operation_ids = {
        operation["operationId"]
        for methods in paths.values()
        if isinstance(methods, dict)
        for operation in methods.values()
        if isinstance(operation, dict) and isinstance(operation.get("operationId"), str)
    }
    assert not any("LocalRecipe" in operation_id for operation_id in operation_ids)


def test_managed_catalog_sync_response_allows_catalogs_over_256_rows() -> None:
    problems = [
        ManagedCatalogSyncProblem(
            recipe_uri=f"https://example.test/recipes/{index}",
            code="catalog.import_failed",
            detail="synthetic test problem",
        )
        for index in range(257)
    ]
    response = ManagedCatalogSyncResponse(
        sync_id="00000000-0000-4000-8000-000000000001",
        request_key="00000000-0000-4000-8000-000000000002",
        trigger="manual",
        state="partial",
        repository="example/recipes",
        commit=None,
        expected_commit=None,
        total_count=257,
        processed_count=257,
        imported_count=0,
        updated_count=0,
        unchanged_count=0,
        skipped_count=257,
        withdrawn_count=0,
        withdrawn_recipes=[],
        stale_recipes=[],
        problems=problems,
        created_at="2026-09-06T00:00:00+00:00",
        completed_at=None,
    )

    assert response.total_count == 257
    assert len(response.problems) == 257
    schema = ManagedCatalogSyncResponse.model_json_schema()
    assert "maxItems" not in schema["properties"]["problems"]
    assert "maxItems" not in schema["properties"]["withdrawn_recipes"]
    assert "maxItems" not in schema["properties"]["stale_recipes"]


def test_managed_sync_maps_reader_failure_to_bounded_problem() -> None:
    client = _sync_client(
        RecipeLibraryError("recipe_package.unavailable", "reader detail " + "x" * 400)
    )
    response = client.post(
        "/api/v1/catalog/managed-recipes/sync",
        json={"request_key": str(uuid.uuid4())},
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "recipe_package.unavailable",
        "detail": "reader detail " + "x" * (256 - len("reader detail ")),
        "request_id": "00000000-0000-4000-8000-000000000001",
    }
    transient = _sync_client(
        RecipeLibraryError("recipe_library.transient", "retryable reader failure")
    ).post(
        "/api/v1/catalog/managed-recipes/sync",
        json={"request_key": str(uuid.uuid4())},
    )
    assert transient.status_code == 503
    assert transient.json()["code"] == "recipe_library.transient"


def test_managed_sync_maps_invalid_reader_and_catalog_errors() -> None:
    invalid = _sync_client(
        RecipeLibraryError("recipe_package.response_invalid", "invalid package")
    ).post(
        "/api/v1/catalog/managed-recipes/sync",
        json={"request_key": str(uuid.uuid4())},
    )
    conflict = _sync_client(
        CatalogConflict("catalog.document_exists", "catalog conflict")
    ).post(
        "/api/v1/catalog/managed-recipes/sync",
        json={"request_key": str(uuid.uuid4())},
    )
    plain_catalog = _sync_client(
        CatalogError("catalog.storage_invalid", "catalog invalid")
    ).post(
        "/api/v1/catalog/managed-recipes/sync",
        json={"request_key": str(uuid.uuid4())},
    )
    in_progress = _sync_client(
        CatalogSyncError("catalog.sync_in_progress", "another sync is running")
    ).post(
        "/api/v1/catalog/managed-recipes/sync",
        json={"request_key": str(uuid.uuid4())},
    )

    assert invalid.status_code == 422
    assert invalid.json()["code"] == "recipe_package.response_invalid"
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "catalog.document_exists"
    assert plain_catalog.status_code == 422
    assert plain_catalog.json()["code"] == "catalog.storage_invalid"
    assert in_progress.status_code == 409
    assert in_progress.json()["code"] == "catalog.sync_in_progress"
