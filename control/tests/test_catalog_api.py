from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError
from vonk_agent_protocol import CatalogSyncState
from vonk_control.auth import Actor
from vonk_control.catalog_api import (
    ManagedCatalogSyncProblem,
    ManagedCatalogSyncResponse,
    install_catalog_routes,
)


def _administrator() -> Actor:
    return Actor("test", "administrator")


def test_catalog_status_is_unavailable_without_a_sync_service() -> None:
    from fastapi.testclient import TestClient

    app = FastAPI()
    install_catalog_routes(app, actor_dependency=Depends(_administrator), service=None)
    response = TestClient(app).get("/api/catalog/managed-recipes/sync-status")
    assert response.status_code == 503


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
        state=CatalogSyncState.PARTIAL,
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

    from datetime import datetime

    from fastapi.testclient import TestClient
    from vonk_control.catalog_sync import CatalogSyncView

    from cluster_profiles.generated_control.models.managed_catalog_sync_response import (
        ManagedCatalogSyncResponse as ClientSyncResponse,
    )

    class Sync:
        def latest(self) -> CatalogSyncView:
            return CatalogSyncView(
                id=response.sync_id,
                request_key=response.request_key,
                trigger=response.trigger,
                state=response.state,
                repository=response.repository,
                commit=None,
                expected_commit=None,
                library_version=None,
                library_updated_at=None,
                total_count=response.total_count,
                processed_count=response.processed_count,
                imported_count=0,
                updated_count=0,
                unchanged_count=0,
                skipped_count=response.skipped_count,
                withdrawn_count=0,
                withdrawn_recipes=(),
                stale_recipes=(),
                problems=tuple(problems),
                created_at=datetime.fromisoformat(response.created_at),
                completed_at=None,
                last_error=None,
            )

    app = FastAPI()
    install_catalog_routes(
        app, actor_dependency=Depends(_administrator), service=None, managed_sync=Sync()
    )
    received = TestClient(app).get("/api/catalog/managed-recipes/sync-status")
    assert received.status_code == 200
    consumed = ClientSyncResponse.from_dict(received.json())
    assert consumed.total_count == 257
    assert [problem.recipe_uri for problem in consumed.problems] == [
        problem.recipe_uri for problem in problems
    ]
    assert consumed.imported_count == 0
    assert consumed.commit is None


def test_catalog_json_contract_rejects_coercion_and_top_level_extras() -> None:
    with pytest.raises(ValidationError):
        ManagedCatalogSyncResponse.model_validate(
            {
                "schema_version": 1,
                "sync_id": "00000000-0000-4000-8000-000000000001",
                "request_key": "00000000-0000-4000-8000-000000000002",
                "trigger": "manual",
                "state": "current",
                "repository": "example/recipes",
                "commit": None,
                "expected_commit": None,
                "total_count": "1",
                "processed_count": 1,
                "imported_count": 1,
                "updated_count": 0,
                "unchanged_count": 0,
                "skipped_count": 0,
                "withdrawn_count": 0,
                "withdrawn_recipes": [],
                "stale_recipes": [],
                "problems": [],
                "created_at": "2026-09-06T00:00:00+00:00",
                "completed_at": None,
                "unexpected": True,
            }
        )
