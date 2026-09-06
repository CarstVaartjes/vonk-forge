from __future__ import annotations

from fastapi import FastAPI
from vonk_control.recipe_image_availability import RecipeImageAvailabilityView
from vonk_control.recipe_image_availability_api import (
    _progress,
    _view_document,
    install_recipe_image_availability_routes,
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
                "model_versions": ["model"],
            },
        },
        failure=None,
        supported_actions=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        model_child={
            "id": "model-child",
            "state": "succeeded",
            "progress": {"phase": "download", "completed_bytes": 0, "total_bytes_known": False},
        },
    )
    response = _view_document(view)
    assert response.result is not None
    assert response.result.model_child_id == "model-child"
    assert {child.kind for child in response.children} == {"model-cache", "runtime-image"}


def test_openapi_uses_typed_recipe_models_and_conflict_schema() -> None:
    app = FastAPI()
    install_recipe_image_availability_routes(app, actor_dependency=lambda: None, service=None)
    schema = app.openapi()
    start = schema["paths"]["/api/v1/library/recipe-image-availability"]["post"]
    listing = schema["paths"]["/api/v1/library/recipe-image-availability"]["get"]
    assert start["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityResponse"
    )
    assert start["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityErrorResponse"
    )
    assert listing["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RecipeImageAvailabilityListResponse"
    )
