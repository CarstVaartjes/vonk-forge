"""Authenticated, typed HTTP routes for Recipe Make available."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, field_validator

from .auth import CursorCodec
from .model_cache_contract import Digest
from .operation_api import bounded_error_responses
from .operation_contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
    OperationProgress,
)
from .recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    RecipeImageAvailabilityView,
)
from .strict_json import StrictJSONModel


class RecipeImageAvailabilityStart(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: str = Field(min_length=1, max_length=36)
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    force: bool = False


class RecipeImageAvailabilityRetry(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: str = Field(min_length=1, max_length=36)


class RecipeImageAvailabilityArtifact(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(min_length=1, max_length=256)
    id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=1024)
    kind: str = Field(min_length=1, max_length=64)
    repository: str | None = None
    source: str = Field(min_length=1, max_length=1024)
    revision: str | None = Field(default=None, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_bytes: int = Field(ge=0)
    roles: list[str]
    model_content_sha256: str | None = None


class RecipeImageAvailabilityChild(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["model-cache", "runtime-image"]
    id: str
    request_key: str | None = None
    state: Literal["queued", "running", "partial", "succeeded", "failed"]
    artifact_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_content_digests: list[Digest]
    artifacts: list[RecipeImageAvailabilityArtifact] = Field(default_factory=list)
    progress: OperationProgress
    failure: AvailabilityOperationFailure | None = None


class RecipeImageAvailabilityAction(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: AvailabilityRecoveryAction

    @field_validator("key", mode="before")
    @classmethod
    def parse_key(cls, value: object) -> object:
        if isinstance(value, AvailabilityRecoveryAction):
            return value
        try:
            return AvailabilityRecoveryAction(value)
        except (TypeError, ValueError) as error:
            raise ValueError("key contains an invalid action") from error


class RecipeImageAvailabilityResult(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    recipe_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_digest: str | None = None
    model_child_id: str | None = None
    artifact_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_content_digests: list[Digest]
    build_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: str = Field(min_length=1, max_length=64)
    registry_manifest_digest: str | None = None
    platform_manifest_digest: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(min_length=1, max_length=256)
    local_image_config_id: str | None = None
    oci_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_bytes: int = Field(ge=1)
    build_id: str | None = None


class RecipeImageAvailabilityResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    kind: Literal["recipe.image.availability.v2"]
    state: Literal["queued", "running", "partial", "succeeded", "failed"]
    attempt: int = Field(ge=0)
    recipe_revision_id: str
    recipe_content_sha256: str
    progress: OperationProgress
    children: list[RecipeImageAvailabilityChild] = Field(default_factory=list)
    result: RecipeImageAvailabilityResult | None = None
    failure: AvailabilityOperationFailure | None = None
    actions: list[RecipeImageAvailabilityAction] = Field(default_factory=list)
    created_at: str
    updated_at: str


class RecipeImageAvailabilityListResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    operations: list[RecipeImageAvailabilityResponse]
    total: int = Field(ge=0)
    next_cursor: str | None = None


class RecipeImageAvailabilityErrorResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    failure: AvailabilityOperationFailure


RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS = {
    ("post", "/api/v1/library/recipe-image-availability"): "startRecipeImageAvailability",
    ("get", "/api/v1/library/recipe-image-availability"): "listRecipeImageAvailability",
    ("get", "/api/v1/library/recipe-image-availability/{operation_id}"):
        "getRecipeImageAvailability",
    ("post", "/api/v1/library/recipe-image-availability/{operation_id}/retry"):
        "retryRecipeImageAvailability",
}


def _failure_document(error: RecipeImageAvailabilityError) -> dict[str, object]:
    retry_time = None
    if isinstance(error.retry_time, str):
        retry_time = error.retry_time
    elif error.retry_after_seconds is not None:
        retry_time = (datetime.now(UTC) + timedelta(seconds=error.retry_after_seconds)).isoformat()
    return AvailabilityOperationFailure.model_validate(
        {
            "code": str(error.code),
            "detail": str(error.detail)[:512],
            "recovery_actions": list(error.recovery_actions),
            "retryable": bool(error.retryable),
            "retry_time": retry_time,
            "retry_after_seconds": error.retry_after_seconds,
            "log_excerpt": error.log_excerpt[:1024]
            if isinstance(error.log_excerpt, str)
            else None,
            "required_bytes": error.required_bytes,
            "free_bytes": error.free_bytes,
            "shortfall_bytes": error.shortfall_bytes,
        }
    ).model_dump(mode="json")


def _failure_response(error: RecipeImageAvailabilityError) -> RecipeImageAvailabilityErrorResponse:
    """Build the complete documented conflict response through its model."""

    return RecipeImageAvailabilityErrorResponse(
        failure=AvailabilityOperationFailure.model_validate(_failure_document(error))
    )


def _progress(value: object) -> OperationProgress:
    raw = dict(value) if isinstance(value, dict) else {"phase": "prepare"}
    # ModelCache uses transfer-specific counters; map them at this boundary
    # into the shared progress contract without leaking provider fields.
    if "completed_bytes" not in raw and isinstance(raw.get("downloaded_bytes"), int):
        raw["completed_bytes"] = raw["downloaded_bytes"]
    if "total_bytes" not in raw and isinstance(raw.get("expected_bytes"), int):
        raw["total_bytes"] = raw["expected_bytes"]
    raw.setdefault("phase", "download")
    raw.setdefault("total_bytes_known", raw.get("total_bytes") is not None)
    return OperationProgress.model_validate({
        key: raw[key]
        for key in (
            "phase", "completed_bytes", "total_bytes", "bytes_per_second",
            "eta_seconds", "total_bytes_known", "checkpoint", "members",
        )
        if key in raw
    })


def _child(value: object, *, kind: Literal["model-cache", "runtime-image"]) -> RecipeImageAvailabilityChild:
    raw = dict(value) if isinstance(value, dict) else {}
    if kind == "runtime-image":
        raw["model_content_digests"] = []
    return RecipeImageAvailabilityChild.model_validate(
        raw | {"kind": kind, "progress": _progress(raw.get("progress"))}
    )


def _view_document(view: RecipeImageAvailabilityView) -> RecipeImageAvailabilityResponse:
    document = view.document()
    result = document.get("result")
    result_model = None
    if isinstance(result, dict):
        child = result.get("model_child")
        result_payload = dict(result)
        result_payload.pop("model_child", None)
        result_model = RecipeImageAvailabilityResult.model_validate(
            result_payload
            | {
                "model_child_id": child.get("id") if isinstance(child, dict) else None,
                "artifact_set_sha256": child.get("artifact_set_sha256") if isinstance(child, dict) else None,
                "model_content_digests": (
                    child["model_content_digests"]
                    if isinstance(child, dict)
                    else []
                ),
            }
        )
    failure = document.get("failure")
    return RecipeImageAvailabilityResponse(
        id=str(document["id"]), request_id=str(document["request_id"]), kind=str(document["kind"]),
        state=str(document["state"]), attempt=int(document["attempt"]),
        recipe_revision_id=str(document["recipe_revision_id"]),
        recipe_content_sha256=str(document["recipe_content_sha256"]),
        progress=_progress(document.get("progress")),
        children=(
            ([_child(view.model_child, kind="model-cache")] if view.model_child is not None else [])
            + [_child({
                "id": view.id,
                "request_key": view.request_id,
                "state": view.image_state or view.state,
                "progress": view.image_progress or {"phase": "prepare"},
                "failure": view.image_failure,
            }, kind="runtime-image")]
        ),
        result=result_model,
        failure=AvailabilityOperationFailure.model_validate(failure) if isinstance(failure, dict) else None,
        actions=[RecipeImageAvailabilityAction(key=str(item)) for item in document.get("supported_actions", [])],
        created_at=str(document["created_at"]), updated_at=str(document["updated_at"]),
    )


def _service(service: RecipeImageAvailabilityService | None) -> RecipeImageAvailabilityService:
    if service is None:
        raise HTTPException(status_code=503, detail="recipe image availability is unavailable")
    return service


def _mutating(actor: Any) -> None:
    if getattr(actor, "role", None) not in {"operator", "administrator"}:
        raise HTTPException(status_code=403, detail="insufficient role")


def install_recipe_image_availability_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: RecipeImageAvailabilityService | None,
    cursor_codec: CursorCodec | None = None,
) -> None:
    """Install the typed schema-2 Recipe Make available API."""

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS)

    @app.post(
        "/api/v1/library/recipe-image-availability",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecipeImageAvailabilityResponse,
        responses=bounded_error_responses(401, 403, 409, 422, 503)
        | {409: {"model": RecipeImageAvailabilityErrorResponse}},
        operation_id="startRecipeImageAvailability",
    )
    def start(body: RecipeImageAvailabilityStart, _request: Request, actor: Any = actor_dependency):
        _mutating(actor)
        try:
            return _view_document(_service(service).start(body.recipe_revision_id, actor=actor.subject, request_id=body.request_key, force=body.force))
        except RecipeImageAvailabilityError as error:
            return JSONResponse(status_code=409, content=_failure_response(error).model_dump(mode="json"))

    @app.get(
        "/api/v1/library/recipe-image-availability",
        response_model=RecipeImageAvailabilityListResponse,
        responses=bounded_error_responses(400, 401, 422, 503),
        operation_id="listRecipeImageAvailability",
    )
    def list_operations(
        recipe_revision_id: Annotated[str | None, Query(max_length=128)] = None,
        state: Annotated[Literal["queued", "running", "partial", "succeeded", "failed"] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        _actor: Any = actor_dependency,
    ):
        context = {"recipe_revision_id": recipe_revision_id, "state": state, "limit": limit}
        boundary = None
        if cursor is not None:
            if cursor_codec is None:
                raise HTTPException(status_code=503, detail="availability cursor unavailable")
            try:
                raw = cursor_codec.decode(cursor, resource="recipe-image-availability", order="created-at-desc/id-desc/v1", context=context)
                if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(item, str) for item in raw):
                    raise ValueError
                boundary = (raw[0], raw[1])
            except ValueError:
                raise HTTPException(status_code=400, detail="cursor is invalid") from None
        try:
            rows, total, next_boundary = _service(service).list_page(recipe_revision_id=recipe_revision_id, state=state, limit=limit, boundary=boundary)
        except ValueError:
            raise HTTPException(status_code=400, detail="cursor is invalid") from None
        next_cursor = None
        if next_boundary is not None and cursor_codec is not None:
            next_cursor = cursor_codec.encode(resource="recipe-image-availability", order="created-at-desc/id-desc/v1", context=context, boundary=list(next_boundary))
        return RecipeImageAvailabilityListResponse(operations=[_view_document(row) for row in rows], total=total, next_cursor=next_cursor)

    @app.get(
        "/api/v1/library/recipe-image-availability/{operation_id}",
        response_model=RecipeImageAvailabilityResponse,
        responses=bounded_error_responses(401, 404, 422, 503)
        | {409: {"model": RecipeImageAvailabilityErrorResponse}},
        operation_id="getRecipeImageAvailability",
    )
    def get(operation_id: str = Path(min_length=1, max_length=64), _actor: Any = actor_dependency):
        try:
            return _view_document(_service(service).get(operation_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe image availability operation not found") from None

    @app.post(
        "/api/v1/library/recipe-image-availability/{operation_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecipeImageAvailabilityResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503)
        | {409: {"model": RecipeImageAvailabilityErrorResponse}},
        operation_id="retryRecipeImageAvailability",
    )
    def retry(body: RecipeImageAvailabilityRetry, _request: Request, operation_id: str = Path(min_length=1, max_length=64), actor: Any = actor_dependency):
        _mutating(actor)
        try:
            return _view_document(_service(service).retry(operation_id, actor=actor.subject, request_id=body.request_key))
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe image availability operation not found") from None
        except RecipeImageAvailabilityError as error:
            return JSONResponse(status_code=409, content=_failure_response(error).model_dump(mode="json"))


__all__ = [
    "RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS",
    "RecipeImageAvailabilityResponse",
    "RecipeImageAvailabilityRetry",
    "RecipeImageAvailabilityStart",
    "install_recipe_image_availability_routes",
]
