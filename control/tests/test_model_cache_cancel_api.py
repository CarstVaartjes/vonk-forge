"""Current singular model cache operator routes cancel before eviction."""

from datetime import UTC, datetime
from unittest.mock import Mock

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from vonk_control.auth import MUTATION_ROLES, Actor
from vonk_control.model_cache import CacheOperationView
from vonk_control.model_cache_api import install_model_operator_routes
from vonk_control.model_cache_contract import (
    ModelCacheOperatorResponse,
    ModelCacheRemovalResult,
)
from vonk_control.model_cache_progress import cache_progress

OPERATION_ID = "00000000-0000-4000-8000-000000000001"
REQUEST_KEY = "00000000-0000-4000-8000-000000000002"


def _client(service, role="administrator"):
    app = FastAPI()
    MUTATION_ROLES.setdefault(
        ("POST", "/api/model/{selector}/remove"), frozenset({"operator", "administrator"})
    )

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
    operation.state = "succeeded"
    operation.progress = cache_progress(
        {"phase": "completed", "completed_artifacts": 1,
         "total_artifacts": 1, "downloaded_bytes": 10, "expected_bytes": 10},
        previous=None,
        now=datetime(2026, 9, 10, tzinfo=UTC),
    )
    operation.result = ModelCacheRemovalResult(
        schema_version=2, removed_entries=[], reclaimed_bytes=10,
        cancelled_operations=[],
    )
    operation.failure = None
    operation.retryable = False
    service = Mock()
    service.remove_model_selector.return_value = operation
    response = _client(service).post(
        "/api/model/model/remove",
        json={"request_key": REQUEST_KEY},
    )
    assert response.status_code == 202, response.text
    parsed = ModelCacheOperatorResponse.model_validate_json(response.content)
    assert parsed.action == "remove"
    service.remove_model_selector.assert_called_once_with(
        "model", actor="test", request_key=REQUEST_KEY
    )


def test_model_operation_observation_is_readable_by_any_authenticated_actor():
    operation = Mock(spec=CacheOperationView)
    operation.id = OPERATION_ID
    operation.request_key = REQUEST_KEY
    operation.state = "succeeded"
    operation.progress = cache_progress(
        {"phase": "completed", "completed_artifacts": 1,
         "total_artifacts": 1, "downloaded_bytes": 10, "expected_bytes": 10},
        previous=None, now=datetime(2026, 9, 10, tzinfo=UTC),
    )
    operation.result = ModelCacheRemovalResult(
        schema_version=2, removed_entries=[], reclaimed_bytes=10,
        cancelled_operations=[],
    )
    operation.failure = None
    operation.retryable = False
    service = Mock()
    service.get_operator_operation.return_value = (operation, "remove", "model")
    response = _client(service, role="viewer").get(
        f"/api/model/operations/{OPERATION_ID}"
    )
    assert response.status_code == 200, response.text
    assert ModelCacheOperatorResponse.model_validate_json(response.content).action == "remove"


def test_model_operator_routes_have_one_current_namespace():
    service = Mock()
    schema = _client(service).get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/model/{selector}/download" in paths
    assert "/api/model/{selector}/remove" in paths
    assert paths["/api/model/operations/{operation_id}"]["get"]["operationId"] == "getModelOperation"
    assert all(path.startswith("/api/model/") for path in paths)
    assert not any(path.startswith("/api/model-cache") for path in paths)
