from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError
from vonk_control.auth import Actor
from vonk_control.recipe_availability_intent import RecipeRevisionIntent
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityView,
)
from vonk_control.recipe_image_availability_api import (
    RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS,
    RecipeCancellationRequest,
    RecipeOperatorRequest,
    RecipeUpdateRequest,
    _child,
    _progress,
    _view_document,
    install_recipe_operator_routes,
)
from vonk_control.recipe_lifecycle_contract import RecipeOperationCancellationResult


@pytest.mark.parametrize(
    "mutation", [{}, {"model_content_digests": [12]}, {"model_content_digests": None}]
)
def test_model_child_requires_typed_model_references(
    mutation: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _child(
            {"id": "model-child", "state": "queued", "progress": {"phase": "download"}}
            | mutation,
            kind="model-cache",
        )


def test_model_cache_progress_maps_to_shared_typed_progress() -> None:
    progress = _progress(
        {
            "schema_version": 2,
            "downloaded_bytes": 40,
            "expected_bytes": 100,
            "completed_artifacts": 1,
            "total_artifacts": 2,
            "current_artifact_key": "weights",
        }
    )
    assert progress.completed_bytes == 40
    assert progress.total_bytes == 100
    assert progress.total_bytes_known is True


@pytest.mark.parametrize("value", [None, [], "prepare", 7])
def test_progress_rejects_non_object_persisted_values(value: object) -> None:
    with pytest.raises((TypeError, ValidationError)):
        _progress(value)


def test_optional_missing_image_progress_does_not_create_a_fake_child() -> None:
    view = RecipeImageAvailabilityView(
        id="operation",
        request_id="r" * 36,
        request=RecipeRevisionIntent(recipe_revision_id="revision"),
        kind="recipe.image.availability.v2",
        state="queued",
        attempt=1,
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        model_digest=None,
        build_input_sha256=None,
        progress={"phase": "prepare", "total_bytes_known": False},
        image_progress=None,
        result=None,
        failure=None,
        supported_actions=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    assert _view_document(view).children == []


def test_completed_result_projection_is_strict_and_exposes_both_children() -> None:
    view = RecipeImageAvailabilityView(
        id="operation",
        request_id="r" * 36,
        request=RecipeRevisionIntent(recipe_revision_id="revision"),
        kind="recipe.image.availability.v2",
        state="succeeded",
        attempt=1,
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        model_digest=None,
        build_input_sha256=None,
        progress={
            "phase": "available",
            "completed_bytes": 20,
            "total_bytes": 20,
            "total_bytes_known": True,
        },
        image_progress={
            "phase": "available",
            "completed_bytes": 20,
            "total_bytes": 20,
            "total_bytes_known": True,
        },
        result={
            "schema_version": 2,
            "recipe_content_sha256": "a" * 64,
            "source": "registry",
            "platform_manifest_digest": "sha256:platform",
            "image_digest": "sha256:image",
            "oci_archive_sha256": "b" * 64,
            "image_bytes": 20,
            "model_child": {
                "id": "model-child",
                "artifact_set_sha256": "c" * 64,
                "model_content_digests": ["d" * 64],
            },
        },
        failure=None,
        supported_actions=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        model_child={
            "id": "model-child",
            "state": "succeeded",
            "model_content_digests": ["d" * 64],
            "progress": {
                "phase": "download",
                "completed_bytes": 0,
                "total_bytes_known": False,
            },
        },
    )
    response = _view_document(view)
    assert response.result is not None
    assert response.result.model_child_id == "model-child"
    assert response.result.model_content_digests == ["d" * 64]
    assert response.children[0].model_content_digests == ["d" * 64]
    assert {child.kind for child in response.children} == {
        "model-cache",
        "runtime-image",
    }


def test_openapi_uses_typed_recipe_models_and_conflict_schema() -> None:
    app = FastAPI()
    install_recipe_operator_routes(app, actor_dependency=lambda: None, service=None)
    schema = app.openapi()
    assert schema["paths"]["/api/recipe/{selector}/download"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith("RecipeDownloadRequest")
    assert schema["paths"]["/api/recipe/{selector}/download"]["post"]["responses"][
        "202"
    ]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityResponse"
    )
    assert schema["paths"]["/api/recipe/{selector}/remove"]["post"]["responses"]["202"][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith("RecipeOperatorResponse")
    assert schema["paths"]["/api/recipe/update"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("RecipeUpdateRequest")
    assert schema["paths"]["/api/recipe/operations/{operation_id}/cancel"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeCancellationRequest"
    )
    assert RecipeOperatorRequest.model_fields.keys() >= {"request_key", "with_model"}
    assert RecipeUpdateRequest.model_fields.keys() >= {
        "request_key",
        "selectors",
        "all",
    }
    assert RecipeCancellationRequest.model_fields.keys() >= {
        "request_key",
        "reason",
    }
    assert "RecipeImageAvailabilityErrorResponse" not in schema["components"]["schemas"]
    assert "RecipeOperatorResponse" in schema["components"]["schemas"]
    for (method, path), operation_id in RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS.items():
        assert schema["paths"][path][method]["operationId"] == operation_id


def test_recipe_operation_observation_is_readable_by_any_authenticated_actor() -> None:
    view = RecipeImageAvailabilityView(
        id="00000000-0000-4000-8000-000000000201",
        request_id="r" * 36,
        request=RecipeRevisionIntent(recipe_revision_id="revision"),
        kind="recipe.image.availability.v2",
        state="queued",
        attempt=1,
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        model_digest=None,
        build_input_sha256=None,
        progress={"phase": "prepare", "total_bytes_known": False},
        image_progress=None,
        result=None,
        failure=None,
        supported_actions=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    service = Mock()
    service.get_operator_operation.return_value = view
    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        service=service,
    )
    response = TestClient(app).get(f"/api/recipe/operations/{view.id}")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == view.id


_REQUEST_KEY = "00000000-0000-4000-8000-000000000001"


def _operator_client(service: Mock) -> TestClient:
    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    return TestClient(app)


def _download(service: Mock) -> Response:
    return _operator_client(service).post(
        "/api/recipe/example/download",
        json={"schema_version": 2, "request_key": _REQUEST_KEY},
    )


def test_download_names_a_transient_availability_refusal() -> None:
    """A retryable dependency failure keeps 503 but names the code and cause."""

    service = Mock()
    service.start_selector.side_effect = RecipeImageAvailabilityError(
        "recipe_image.metadata_refresh_failed",
        "latest recipe metadata could not be refreshed",
        retryable=True,
    )

    response = _download(service)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == (
        "recipe_image.metadata_refresh_failed: "
        "latest recipe metadata could not be refreshed"
    )


def test_download_names_a_terminal_availability_refusal() -> None:
    """A non-retryable refusal is a 409 conflict, not a retryable 503."""

    service = Mock()
    service.start_selector.side_effect = RecipeImageAvailabilityError(
        "recipe_image.recipe_unavailable",
        "selected recipe revision is unavailable or inactive",
    )

    response = _download(service)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "recipe_image.recipe_unavailable: "
        "selected recipe revision is unavailable or inactive"
    )


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (
            RecipeImageAvailabilityError(
                "recipe_image.selector_missing", "recipe selector was not found"
            ),
            404,
            "recipe selector was not found",
        ),
        (
            RecipeImageAvailabilityError(
                "recipe_image.selector_ambiguous",
                "recipe selector matches multiple recipes",
            ),
            409,
            "recipe selector matches multiple recipes",
        ),
        (
            RecipeImageAvailabilityError(
                "recipe_image.request_key_reused", "request key was already used"
            ),
            409,
            "request key was already used",
        ),
        (
            RecipeImageAvailabilityError(
                "runtime_image.receipt_invalid",
                "source-build receipt is not readable",
            ),
            422,
            "source-build receipt is not readable",
        ),
    ],
)
def test_download_keeps_the_special_case_refusals_unchanged(
    error: RecipeImageAvailabilityError, status_code: int, detail: str
) -> None:
    service = Mock()
    service.start_selector.side_effect = error

    response = _download(service)

    assert response.status_code == status_code, response.text
    assert response.json()["detail"] == detail


def test_remove_names_a_terminal_availability_refusal() -> None:
    service = Mock()
    service.remove_selector.side_effect = RecipeImageAvailabilityError(
        "recipe_image.identity_conflict",
        "selected recipe execution identity changed",
    )

    response = _operator_client(service).post(
        "/api/recipe/example/remove",
        json={"schema_version": 2, "request_key": _REQUEST_KEY},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "recipe_image.identity_conflict: selected recipe execution identity changed"
    )


def test_remove_response_projects_the_stored_model_choice_not_the_request() -> None:
    service = Mock()
    service.remove_selector.return_value = {
        "schema_version": 2,
        "action": "remove",
        "selector": "example",
        "request_key": _REQUEST_KEY,
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "recipe_revision_id": "revision-example",
        "with_model": False,
        "state": "succeeded",
        "progress": {
            "phase": "completed",
            "completed_bytes": 0,
            "total_bytes": 0,
            "total_bytes_known": True,
            "completed_items": 0,
            "total_items": 0,
        },
        "reclaimed_bytes": 0,
        "preserved": ["model-download"],
        "next_actions": [],
        "cancelled_operations": [],
        "cancelled_builds": [],
        "model_removals": [],
    }

    response = _operator_client(service).post(
        "/api/recipe/example/remove",
        json={
            "schema_version": 2,
            "request_key": _REQUEST_KEY,
            "with_model": True,
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["with_model"] is False


def test_remove_response_preserves_partial_progress_and_failure() -> None:
    service = Mock()
    service.remove_selector.return_value = {
        "schema_version": 2,
        "action": "remove",
        "selector": "example",
        "request_key": _REQUEST_KEY,
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "recipe_revision_id": "revision-example",
        "with_model": False,
        "state": "partial",
        "progress": {
            "phase": "reclaiming",
            "completed_bytes": 13,
            "total_bytes_known": False,
            "completed_items": 1,
            "total_items": 2,
            "activity": "waiting",
        },
        "reclaimed_bytes": 13,
        "preserved": ["model-download"],
        "next_actions": ["retry"],
        "cancelled_operations": [],
        "cancelled_builds": [],
        "model_removals": [],
        "failure": {
            "code": "runtime_image.removal_storage_failed",
            "detail": "managed image storage is temporarily unavailable",
            "retryable": True,
            "recovery_actions": ["retry"],
            "retry_time": "2026-01-01T00:00:05+00:00",
            "retry_after_seconds": 5,
        },
    }

    response = _operator_client(service).post(
        "/api/recipe/example/remove",
        json={"schema_version": 2, "request_key": _REQUEST_KEY},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "partial"
    assert body["progress"]["phase"] == "reclaiming"
    assert body["progress"]["completed_bytes"] == 13
    assert body["progress"]["total_bytes"] is None
    assert body["failure"]["retryable"] is True
    assert body["next_actions"] == ["retry"]


def test_operation_observation_names_a_transient_availability_refusal() -> None:
    service = Mock()
    service.get_operator_operation.side_effect = RecipeImageAvailabilityError(
        "recipe_image.model_cache_unavailable",
        "exact Model artifact preparation could not be queued",
        retryable=True,
    )

    response = _operator_client(service).get(
        "/api/recipe/operations/00000000-0000-4000-8000-000000000002"
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == (
        "recipe_image.model_cache_unavailable: "
        "exact Model artifact preparation could not be queued"
    )


def test_availability_refusal_detail_is_redacted_and_bounded() -> None:
    service = Mock()
    service.start_selector.side_effect = RecipeImageAvailabilityError(
        "recipe_image.metadata_refresh_failed",
        "token=swordfish " + "x" * 200,
        retryable=True,
    )

    detail = _download(service).json()["detail"]

    assert "swordfish" not in detail
    assert detail.startswith("recipe_image.metadata_refresh_failed: token=<redacted>")
    assert len(detail) == len("recipe_image.metadata_refresh_failed: ") + 80


def test_recipe_cancel_requires_mutation_role_and_returns_durable_request() -> None:
    cancellation = RecipeOperationCancellationResult(
        cancel_requested=True,
        cancel_requested_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        cancel_request_id="00000000-0000-4000-8000-000000000221",
        cancel_actor="operator",
        reason="stop preparation",
    )
    view = RecipeImageAvailabilityView(
        id="00000000-0000-4000-8000-000000000220",
        request_id="00000000-0000-4000-8000-000000000219",
        request=RecipeRevisionIntent(recipe_revision_id="revision"),
        kind="recipe.image.availability.v2",
        state="cancelling",
        attempt=1,
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        model_digest=None,
        build_input_sha256=None,
        progress={"phase": "prepare", "total_bytes_known": False},
        image_progress=None,
        result=None,
        failure=None,
        supported_actions=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        cancellation=cancellation,
    )
    service = Mock()
    service.cancel.return_value = view
    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    response = TestClient(app).post(
        f"/api/recipe/operations/{view.id}/cancel",
        json={
            "schema_version": 2,
            "request_key": cancellation.cancel_request_id,
            "reason": cancellation.reason,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "cancelling"
    assert response.json()["cancellation"]["cancel_request_id"] == (
        cancellation.cancel_request_id
    )
    service.cancel.assert_called_once_with(
        view.id,
        actor="operator",
        request_id=cancellation.cancel_request_id,
        reason=cancellation.reason,
    )

    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        service=service,
    )
    denied = TestClient(app).post(
        f"/api/recipe/operations/{view.id}/cancel",
        json={
            "schema_version": 2,
            "request_key": cancellation.cancel_request_id,
            "reason": cancellation.reason,
        },
    )
    assert denied.status_code == 403
    service.cancel.assert_called_once()
