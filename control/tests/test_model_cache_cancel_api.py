"""Current singular model cache operator routes cancel before eviction."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_control.auth import Actor
from vonk_control.cache_removal_review import (
    CacheRemovalReviewContent,
    seal_cache_removal_review,
)
from vonk_control.model_cache import CacheOperationView
from vonk_control.model_cache_api import install_model_operator_routes
from vonk_control.model_cache_contract import (
    ModelCacheCancellation,
    ModelCacheCounters,
    ModelCacheOperatorResponse,
    ModelCacheRemovalResult,
)
from vonk_control.model_cache_progress import cache_progress, progress_document

OPERATION_ID = "00000000-0000-4000-8000-000000000001"
REQUEST_KEY = "00000000-0000-4000-8000-000000000002"
CANCEL_KEY = "00000000-0000-4000-8000-000000000003"
MODEL_CONTENT_SHA256 = "a" * 64
REVIEW_DIGEST = "b" * 64


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
    )
    return TestClient(app)


def _operation() -> CacheOperationView:
    """The producer's actual view supplies defaults for optional observations."""
    now = datetime(2026, 9, 10, tzinfo=UTC).isoformat()
    return CacheOperationView(
        id=OPERATION_ID,
        request_key=REQUEST_KEY,
        kind="remove",
        state=LifecycleState.SUCCEEDED.value,
        attempt=1,
        model_content_sha256=None,
        artifact_set_sha256=None,
        plan_digest=None,
        review_digest=None,
        progress={},
        result=None,
        last_error=None,
        created_at=now,
        updated_at=now,
        completed_at=now,
    )


def test_remove_is_the_current_model_eviction_boundary():
    operation = replace(
        _operation(),
        model_content_sha256=MODEL_CONTENT_SHA256,
        review_digest=REVIEW_DIGEST,
        state=LifecycleState.SUCCEEDED.value,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "completed",
                        "completed_artifacts": 1,
                        "total_artifacts": 1,
                        "downloaded_bytes": 10,
                        "expected_bytes": 10,
                    }
                ),
                previous=None,
                now=datetime(2026, 9, 10, tzinfo=UTC),
            )
        ),
        result=ModelCacheRemovalResult(
            schema_version=2,
            removed_entries=[],
            reclaimed_bytes=10,
            cancelled_operations=[],
        ),
        failure=None,
        retryable=False,
        blockers=(),
        next_attempt_at=None,
        cancellation=None,
    )
    service = Mock()
    service.remove_model_selector.return_value = operation
    response = _client(service).post(
        "/api/model/model/remove",
        json={
            "request_key": REQUEST_KEY,
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
    )
    for body in (
        {},
        {
            "request_key": REQUEST_KEY,
            "review_digest": REVIEW_DIGEST,
        },
    ):
        refused = _client(service).post("/api/model/model/remove", json=body)
        assert refused.status_code == 422
    assert service.remove_model_selector.call_count == 1


def test_remove_review_is_read_only_and_requires_operator_role():
    review = seal_cache_removal_review(
        CacheRemovalReviewContent(
            resource_kind="model",
            selector="model",
            target_identity=MODEL_CONTENT_SHA256,
            with_model=None,
            assets=[],
            references=[],
            active_work=[],
            blockers=[],
            observed_at="2026-09-24T10:00:00Z",
        )
    )
    service = Mock()
    service.review_model_removal.return_value = review

    response = _client(service).get("/api/model/model/remove-review")
    assert response.status_code == 200, response.text
    assert response.json()["review_digest"] == review.review_digest
    service.review_model_removal.assert_called_once_with("model")
    service.remove_model_selector.assert_not_called()

    refused = _client(service, role="viewer").get("/api/model/model/remove-review")
    assert refused.status_code == 403


def test_model_operation_observation_is_readable_by_any_authenticated_actor():
    operation = replace(
        _operation(),
        model_content_sha256=None,
        review_digest=None,
        state=LifecycleState.SUCCEEDED.value,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "completed",
                        "completed_artifacts": 1,
                        "total_artifacts": 1,
                        "downloaded_bytes": 10,
                        "expected_bytes": 10,
                    }
                ),
                previous=None,
                now=datetime(2026, 9, 10, tzinfo=UTC),
            )
        ),
        result=ModelCacheRemovalResult(
            schema_version=2,
            removed_entries=[],
            reclaimed_bytes=10,
            cancelled_operations=[],
        ),
        failure=None,
        retryable=False,
        blockers=(),
        next_attempt_at=None,
        cancellation=None,
    )
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
    operation = replace(
        _operation(),
        model_content_sha256=None,
        review_digest=None,
        state=LifecycleState.OBSERVING,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "cancelling",
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                        "downloaded_bytes": 12,
                        "expected_bytes": 20,
                    }
                ),
                previous=None,
                now=datetime(2026, 9, 10, tzinfo=UTC),
            )
        ),
        result=None,
        failure=None,
        retryable=False,
        blockers=(),
        next_attempt_at=None,
        cancellation=ModelCacheCancellation(
            request_key=CANCEL_KEY,
            actor="test",
            reason="operator stopped this download",
            requested_at="2026-09-10T00:00:00+00:00",
        ).model_dump(mode="json"),
    )
    service = Mock()
    service.cancel_operation.return_value = operation
    service.get_operator_operation.return_value = (operation, "download", "model")
    body = {
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
    assert parsed.state == LifecycleState.OBSERVING
    assert parsed.cancellation == ModelCacheCancellation.model_validate_json(
        canonical_message(operation.cancellation)
    )
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
    assert paths["/api/model/{selector}/remove-review"]["get"]["operationId"] == (
        "reviewModelRemoval"
    )
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
