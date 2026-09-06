"""Authenticated HTTP routes for exact Recipe image/build availability."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)


class RecipeImageAvailabilityStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(min_length=1, max_length=36)
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    force: bool = False


class RecipeImageAvailabilityRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(min_length=1, max_length=36)


def _failure_document(error: RecipeImageAvailabilityError) -> dict[str, object]:
    raw = {
        "code": str(error.code),
        "detail": str(error.detail)[:512],
        "recovery_actions": list(error.recovery_actions),
        "retryable": bool(error.retryable),
        "retry_time": None,
        "retry_after_seconds": error.retry_after_seconds,
        "log_excerpt": error.log_excerpt[:1024] if isinstance(error.log_excerpt, str) else None,
        "required_bytes": None,
        "free_bytes": None,
        "shortfall_bytes": None,
    }
    # The model-cache owner supplies the shared schema class.  Keeping this
    # import at call time lets this route remain importable during a rolling
    # deploy, while every deployed response is validated by that class.
    try:
        from .operation_contract import AvailabilityOperationFailure
    except ImportError:
        return raw
    return AvailabilityOperationFailure.model_validate(raw).model_dump(mode="json")


def _view_document(view: Any) -> dict[str, object]:
    document = view.document()
    failure = document.get("failure")
    if not isinstance(failure, dict):
        return document
    try:
        from .operation_contract import AvailabilityOperationFailure
    except ImportError:
        return document
    document["failure"] = AvailabilityOperationFailure.model_validate(failure).model_dump(mode="json")
    return document


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
) -> None:
    """Install the schema-2 API without exposing distribution-to-Spark routes."""

    @app.post(
        "/api/v1/library/recipe-image-availability",
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="startRecipeImageAvailability",
    )
    def start(
        body: RecipeImageAvailabilityStart,
        _request: Request,
        actor: Any = actor_dependency,
    ) -> dict[str, object]:
        _mutating(actor)
        try:
            view = _service(service).start(
                body.recipe_revision_id,
                actor=actor.subject,
                request_id=body.request_key,
                force=body.force,
            )
            return _view_document(view)
        except RecipeImageAvailabilityError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"schema_version": 2, "failure": _failure_document(error)},
            )

    @app.get(
        "/api/v1/library/recipe-image-availability/{operation_id}",
        operation_id="getRecipeImageAvailability",
    )
    def get(
        operation_id: str = Path(min_length=1, max_length=64),
        _actor: Any = actor_dependency,
    ) -> dict[str, object]:
        try:
            return _view_document(_service(service).get(operation_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe image availability operation not found") from None

    @app.post(
        "/api/v1/library/recipe-image-availability/{operation_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="retryRecipeImageAvailability",
    )
    def retry(
        body: RecipeImageAvailabilityRetry,
        _request: Request,
        operation_id: str = Path(min_length=1, max_length=64),
        actor: Any = actor_dependency,
    ) -> dict[str, object]:
        _mutating(actor)
        try:
            return _view_document(_service(service).retry(
                operation_id,
                actor=actor.subject,
                request_id=body.request_key,
            ))
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe image availability operation not found") from None
        except RecipeImageAvailabilityError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"schema_version": 2, "failure": _failure_document(error)},
            )


__all__ = [
    "RecipeImageAvailabilityRetry",
    "RecipeImageAvailabilityStart",
    "install_recipe_image_availability_routes",
]
