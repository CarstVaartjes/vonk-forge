"""Current singular model cache operator routes cancel before eviction."""

from datetime import UTC, datetime
from unittest.mock import Mock

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from vonk_control.auth import Actor
from vonk_control.model_cache import CacheOperationView
from vonk_control.model_cache_api import install_model_operator_routes
from vonk_control.model_cache_contract import (
    ModelCacheCancellation,
    ModelCacheOperatorResponse,
    ModelCacheRemovalResult,
)
from vonk_control.model_cache_progress import cache_progress

OPERATION_ID = "00000000-0000-4000-8000-000000000001"
REQUEST_KEY = "00000000-0000-4000-8000-000000000002"
CANCEL_KEY = "00000000-0000-4000-8000-000000000003"
MODEL_CONTENT_SHA256 = "a" * 64


def _client(service, role="administrator"):
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = REQUEST_KEY
        return await call_next(request)

    install_model_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", role)),
        service=service,
        audits=[],
    )
    return TestClient(app)


def test_remove_is_the_current_model_eviction_boundary():
    operation = Mock(spec=CacheOperationView)
    operation.id = OPERATION_ID
    operation.request_key = REQUEST_KEY
    operation.model_content_sha256 = MODEL_CONTENT_SHA256
    operation.state = "succeeded"
    operation.progress = cache_progress(
        {
            "phase": "completed",
            "completed_artifacts": 1,
            "total_artifacts": 1,
            "downloaded_bytes": 10,
            "expected_bytes": 10,
        },
        previous=None,
        now=datetime(2026, 9, 10, tzinfo=UTC),
    )
    operation.result = ModelCacheRemovalResult(
        schema_version=2,
        removed_entries=[],
        reclaimed_bytes=10,
        cancelled_operations=[],
    )
    operation.failure = None
    operation.retryable = False
    operation.cancellation = None
    service = Mock()
    service.remove_model_selector.return_value = operation
    response = _client(service).post(
        "/api/model/model/remove",
        json={
            "schema_version": 2,
            "request_key": REQUEST_KEY,
            "model_content_sha256": MODEL_CONTENT_SHA256,
        },
    )
    assert response.status_code == 202, response.text
    parsed = ModelCacheOperatorResponse.model_validate_json(response.content)
    assert parsed.action == "remove"
    assert parsed.model_content_sha256 == MODEL_CONTENT_SHA256
    service.remove_model_selector.assert_called_once_with(
        "model",
        actor="test",
        request_key=REQUEST_KEY,
        model_content_sha256=MODEL_CONTENT_SHA256,
    )
    for body in (
        {"schema_version": 2, "request_key": REQUEST_KEY},
        {
            "schema_version": 2,
            "request_key": REQUEST_KEY,
            "model_content_sha256": "not-a-digest",
        },
    ):
        refused = _client(service).post("/api/model/model/remove", json=body)
        assert refused.status_code == 422
    assert service.remove_model_selector.call_count == 1


def test_model_operation_observation_is_readable_by_any_authenticated_actor():
    operation = Mock(spec=CacheOperationView)
    operation.id = OPERATION_ID
    operation.request_key = REQUEST_KEY
    operation.model_content_sha256 = None
    operation.state = "succeeded"
    operation.progress = cache_progress(
        {
            "phase": "completed",
            "completed_artifacts": 1,
            "total_artifacts": 1,
            "downloaded_bytes": 10,
            "expected_bytes": 10,
        },
        previous=None,
        now=datetime(2026, 9, 10, tzinfo=UTC),
    )
    operation.result = ModelCacheRemovalResult(
        schema_version=2,
        removed_entries=[],
        reclaimed_bytes=10,
        cancelled_operations=[],
    )
    operation.failure = None
    operation.retryable = False
    operation.cancellation = None
    service = Mock()
    service.get_operator_operation.return_value = (operation, "remove", "model")
    response = _client(service, role="viewer").get(
        f"/api/model/operations/{OPERATION_ID}"
    )
    assert response.status_code == 200, response.text
    assert (
        ModelCacheOperatorResponse.model_validate_json(response.content).action
        == "remove"
    )


def test_cancel_route_requires_operator_and_returns_durable_intent():
    operation = Mock(spec=CacheOperationView)
    operation.id = OPERATION_ID
    operation.request_key = REQUEST_KEY
    operation.model_content_sha256 = None
    operation.state = "cancelling"
    operation.progress = cache_progress(
        {
            "phase": "cancelling",
            "completed_artifacts": 0,
            "total_artifacts": 1,
            "downloaded_bytes": 12,
            "expected_bytes": 20,
        },
        previous=None,
        now=datetime(2026, 9, 10, tzinfo=UTC),
    )
    operation.result = None
    operation.failure = None
    operation.retryable = False
    operation.cancellation = ModelCacheCancellation(
        request_key=CANCEL_KEY,
        actor="test",
        reason="operator stopped this download",
        requested_at="2026-09-10T00:00:00+00:00",
    )
    service = Mock()
    service.cancel_operation.return_value = operation
    service.get_operator_operation.return_value = (operation, "download", "model")
    body = {
        "schema_version": 2,
        "request_key": CANCEL_KEY,
        "reason": "operator stopped this download",
    }

    denied = _client(service, role="viewer").post(
        f"/api/model/operations/{OPERATION_ID}/cancel", json=body
    )
    assert denied.status_code == 403
    service.cancel_operation.assert_not_called()

    response = _client(service, role="operator").post(
        f"/api/model/operations/{OPERATION_ID}/cancel", json=body
    )
    assert response.status_code == 202, response.text
    parsed = ModelCacheOperatorResponse.model_validate_json(response.content)
    assert parsed.state == "cancelling"
    assert parsed.cancellation == operation.cancellation
    service.cancel_operation.assert_called_once_with(
        OPERATION_ID,
        actor="test",
        request_key=CANCEL_KEY,
        reason="operator stopped this download",
    )


def test_model_operator_routes_have_one_current_namespace():
    service = Mock()
    schema = _client(service).get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/model/{selector}/download" in paths
    assert "/api/model/{selector}/remove" in paths
    assert (
        paths["/api/model/operations/{operation_id}"]["get"]["operationId"]
        == "getModelOperation"
    )
    assert (
        paths["/api/model/operations/{operation_id}/cancel"]["post"]["operationId"]
        == "cancelModelOperation"
    )
    assert all(path.startswith("/api/model/") for path in paths)
    assert not any(path.startswith("/api/model-cache") for path in paths)
