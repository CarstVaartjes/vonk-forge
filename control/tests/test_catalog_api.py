from __future__ import annotations

from fastapi import FastAPI
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.catalog_api import install_catalog_routes


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
    assert "/api/v1/catalog/recipes" not in paths
    operation_ids = {
        operation["operationId"]
        for methods in paths.values()
        if isinstance(methods, dict)
        for operation in methods.values()
        if isinstance(operation, dict) and isinstance(operation.get("operationId"), str)
    }
    assert not any("LocalRecipe" in operation_id for operation_id in operation_ids)
