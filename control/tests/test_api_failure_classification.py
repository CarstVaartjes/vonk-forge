"""API dependency outages may retry; deterministic faults must fail with evidence."""

import json
import logging
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)
from vonk_agent_protocol import ControllerErrorCode
from vonk_agent_protocol.agent_words import AgentClientDecision
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.model_cache_contract import (
    ModelCacheCancellationRequest,
    ModelCacheOperatorRequest,
)
from vonk_control.operation_api import BoundedErrorResponse

REQUEST_KEY = "00000000-0000-4000-8000-000000000002"

# Real installed routes: catches broad error-to-503 mappings, including dispatch
# through a shared mapper. Every model-cache failure() call site is exercised.
CASES = [
    (
        "model_cache",
        "get_operator_request",
        "GET",
        f"/api/model/requests/{REQUEST_KEY}",
    ),
    (
        "model_cache",
        "get_operator_operation",
        "GET",
        f"/api/model/operations/{REQUEST_KEY}",
    ),
    ("model_cache", "review_model_removal", "GET", "/api/model/model/remove-review"),
    ("model_cache", "download_model_selector", "POST", "/api/model/model/download"),
    ("model_cache", "remove_model_selector", "POST", "/api/model/model/remove"),
    (
        "model_cache",
        "cancel_operation",
        "POST",
        f"/api/model/operations/{REQUEST_KEY}/cancel",
    ),
    ("library_projection", "models", "GET", "/api/model/library"),
    ("library_projection", "model_detail", "GET", "/api/model/model"),
    ("library_projection", "recipe_library", "GET", "/api/recipe/library"),
    ("library_projection", "recipe_detail", "GET", "/api/recipe/recipe"),
    ("fleet_profiles", "list", "GET", "/api/profile"),
    ("fleet_profiles", "read_number", "GET", "/api/profile/1"),
    ("fleet_profiles", "definition_number", "GET", "/api/profile/1/definition"),
    ("fleet_profiles", "get_number", "POST", "/api/profile/1/preview"),
    ("fleet_profiles", "progress_number", "GET", "/api/profile/1/progress"),
    (
        "fleet_profiles",
        "application",
        "GET",
        f"/api/profile/applications/{REQUEST_KEY}",
    ),
    (
        "fleet_profiles",
        "application_by_request_key",
        "GET",
        f"/api/profile/1/requests/{REQUEST_KEY}",
    ),
    ("fleet_projection", "read", "GET", "/api/fleet/node"),
    (
        "recipe_image_availability",
        "get_operator_request",
        "GET",
        f"/api/recipe/requests/{REQUEST_KEY}",
    ),
    (
        "recipe_image_availability",
        "get_operator_operation",
        "GET",
        f"/api/recipe/operations/{REQUEST_KEY}",
    ),
    ("failure_evidence", "read", "GET", "/api/operations/operation/evidence?attempt=1"),
]


@pytest.mark.parametrize("surface,member,method,path", CASES)
@pytest.mark.parametrize(
    "fault",
    [
        TypeError,
        ValueError,
        RuntimeError,
        IndexError,
        OSError,
        OperationalError,
        ProgrammingError,
        IntegrityError,
    ],
)
def test_api_errors_are_classified_by_cause(
    surface, member, method, path, fault, caplog, monkeypatch
):
    """Catch deterministic faults masked as retryable outages or lacking a traceback."""
    monkeypatch.setattr(logging.getLogger("vonk_control"), "propagate", True)
    service = Mock()
    error = (
        fault("SELECT", {}, OSError("dependency down"))
        if issubclass(fault, DBAPIError)
        else fault("private password=do-not-emit")
    )
    getattr(service, member).side_effect = error
    codec = TokenCodec(b"k" * 32)
    app = create_app(jobs=Mock(), tokens=codec, now=lambda: 10, **{surface: service})
    token = codec.issue(Actor("operator", "administrator"), ttl_seconds=1000, now=0)
    with caplog.at_level("INFO", logger="vonk_control.api"):
        response = TestClient(app).request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=(
                ModelCacheCancellationRequest(request_key=REQUEST_KEY, reason="test")
                if path.endswith("/cancel")
                else ModelCacheOperatorRequest(request_key=REQUEST_KEY)
            ).model_dump(mode="json")
            if method == "POST"
            else None,
        )
    getattr(service, member).assert_called_once()
    problem = BoundedErrorResponse.model_validate_json(response.content)
    if fault in (OSError, OperationalError):
        assert response.status_code == 503, response.text
        assert response.headers["x-vonk-error-code"] == ControllerErrorCode.UNAVAILABLE
        return
    assert response.status_code == 500, response.text
    assert problem.context is not None
    assert problem.context.code == ControllerErrorCode.INTERNAL_ERROR
    assert problem.context.decision == AgentClientDecision.EXIT
    assert problem.context.retryable is False
    assert problem.context.request_id == response.headers["x-request-id"]
    assert "do-not-emit" not in response.text
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    event = next(
        item for item in events if item.get("request_id") == problem.context.request_id
    )
    assert event["failure_type"] == fault.__name__
    assert event["traceback"]
    assert "do-not-emit" not in json.dumps(event)
