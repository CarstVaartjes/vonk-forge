"""Cancellation uses the same authenticated, typed cache operation boundary."""
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from vonk_control.auth import Actor
from vonk_control.model_cache import (
    CacheOperationView,
    ModelCacheConflict,
    ModelCacheNotFound,
)
from vonk_control.model_cache_api import install_model_cache_routes
from vonk_control.model_cache_contract import ModelCacheOperationResponse
from vonk_control.model_cache_progress import cache_progress

OPERATION_ID = "00000000-0000-4000-8000-000000000001"
PATH = f"/api/v1/model-cache/operations/{OPERATION_ID}/cancel"


def _client(service, role="administrator"):
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = OPERATION_ID
        return await call_next(request)

    audits = []
    install_model_cache_routes(
        app, actor_dependency=Depends(lambda: Actor("test", role)),
        service=service, audits=audits,
    )
    return TestClient(app), audits


def test_cancel_returns_canonical_operation_and_audits_action():
    operation = CacheOperationView(
        id=OPERATION_ID, request_key=OPERATION_ID, kind="download", state="cancelled",
        attempt=1, artifact_set_sha256=None, plan_digest=None, progress=cache_progress(
            {"phase": "downloading", "completed_artifacts": 0, "total_artifacts": 1,
             "downloaded_bytes": 10, "expected_bytes": 100},
            previous=None, now=datetime(2026, 9, 9, tzinfo=UTC),
        ),
        result=None, last_error=None, created_at="2026-09-09T00:00:00Z",
        updated_at="2026-09-09T00:01:00Z", completed_at="2026-09-09T00:01:00Z",
    )
    service = Mock()
    service.cancel_operation.return_value = operation
    client, audits = _client(service)
    response = client.post(PATH)
    assert response.status_code == 200
    parsed = ModelCacheOperationResponse.model_validate_json(response.content)
    assert parsed.id == OPERATION_ID
    assert parsed.state == "cancelled"
    service.cancel_operation.assert_called_once_with(OPERATION_ID)
    assert len(audits) == 1
    schema = client.get("/openapi.json").json()
    endpoint = schema["paths"]["/api/v1/model-cache/operations/{operation_id}/cancel"]["post"]
    assert endpoint["operationId"] == "cancelModelCacheOperation"


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_cancel_requires_administrator(role):
    service = Mock()
    client, audits = _client(service, role)
    assert client.post(PATH).status_code == 403
    service.cancel_operation.assert_not_called()
    assert not audits


@pytest.mark.parametrize("error,status", [
    (ModelCacheNotFound("model_cache.missing", "missing"), 404),
    (ModelCacheConflict("model_cache.conflict", "operation cannot be cancelled"), 409),
])
def test_cancel_preserves_service_error_semantics(error, status):
    service = Mock()
    service.cancel_operation.side_effect = error
    client, audits = _client(service)
    assert client.post(PATH).status_code == status
    assert not audits
