from __future__ import annotations

from fastapi import FastAPI
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.catalog_api import (
    ManagedCatalogSyncProblem,
    ManagedCatalogSyncResponse,
    install_catalog_routes,
)


def _administrator() -> Actor:
    return Actor("administrator", "test")


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
