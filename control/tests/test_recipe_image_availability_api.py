from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from vonk_control.operation_api import admin_openapi_schema
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityView,
)
from vonk_control.recipe_image_availability_api import (
    RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS,
    RecipeImageAvailabilityErrorResponse,
    _child,
    _failure_response,
    _progress,
    _view_document,
    install_recipe_image_availability_routes,
)


@pytest.mark.parametrize("mutation", [{}, {"model_content_digests": [12]}, {"model_content_digests": None}])
def test_model_child_requires_typed_model_references(mutation: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _child(
            {"id": "model-child", "state": "queued", "progress": {"phase": "download"}} | mutation,
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


def test_failure_response_serializes_the_complete_declared_error_model() -> None:
    error = RecipeImageAvailabilityError(
        "recipe_image.build_failed",
        "compiler failed",
        retryable=True,
        recovery_actions=("retry",),
    )

    response = _failure_response(error)

    assert isinstance(response, RecipeImageAvailabilityErrorResponse)
    assert response.model_dump(mode="json")["schema_version"] == 2
    assert response.failure.code == "recipe_image.build_failed"
    assert response.failure.recovery_actions == ["retry"]


@pytest.mark.parametrize("value", [None, [], "prepare", 7])
def test_progress_rejects_non_object_persisted_values(value: object) -> None:
    with pytest.raises((TypeError, ValidationError)):
        _progress(value)


def test_optional_missing_image_progress_does_not_create_a_fake_child() -> None:
    view = RecipeImageAvailabilityView(
        id="operation",
        request_id="r" * 36,
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
        kind="recipe.image.availability.v2",
        state="succeeded",
        attempt=1,
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        model_digest=None,
        build_input_sha256=None,
        progress={"phase": "available", "completed_bytes": 20, "total_bytes": 20, "total_bytes_known": True},
        image_progress={"phase": "available", "completed_bytes": 20, "total_bytes": 20, "total_bytes_known": True},
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
            "progress": {"phase": "download", "completed_bytes": 0, "total_bytes_known": False},
        },
    )
    response = _view_document(view)
    assert response.result is not None
    assert response.result.model_child_id == "model-child"
    assert response.result.model_content_digests == ["d" * 64]
    assert response.children[0].model_content_digests == ["d" * 64]
    assert {child.kind for child in response.children} == {"model-cache", "runtime-image"}


def test_openapi_uses_typed_recipe_models_and_conflict_schema() -> None:
    app = FastAPI()
    install_recipe_image_availability_routes(app, actor_dependency=lambda: None, service=None)
    schema = app.openapi()
    start = schema["paths"]["/api/v1/library/recipe-image-availability"]["post"]
    listing = schema["paths"]["/api/v1/library/recipe-image-availability"]["get"]
    retry = schema["paths"][
        "/api/v1/library/recipe-image-availability/{operation_id}/retry"
    ]["post"]
    assert start["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityResponse"
    )
    assert start["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityErrorResponse"
    )
    assert listing["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityListResponse"
    )
    assert retry["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityErrorResponse"
    )
    admin = admin_openapi_schema(app)
    for (method, path), operation_id in RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS.items():
        assert admin["paths"][path][method]["operationId"] == operation_id
