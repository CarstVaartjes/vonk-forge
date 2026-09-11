"""Authenticated, typed HTTP routes for Recipe Make available."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Request, status
from pydantic import ConfigDict, Field, field_validator, model_validator

from .auth import MUTATION_ROLES
from .bounded_json import integer, require_integer
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
    state: Literal["queued", "running", "partial", "succeeded", "failed", "cancelled"]
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
    state: Literal["queued", "running", "partial", "succeeded", "failed", "cancelled"]
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

    @model_validator(mode="after")
    def terminal_evidence_is_consistent(self) -> RecipeImageAvailabilityResponse:
        if self.state == "succeeded" and (self.result is None or self.failure is not None):
            raise ValueError("successful image availability requires a result and no failure")
        if self.state == "failed" and self.failure is None:
            raise ValueError("failed image availability requires failure evidence")
        if self.state != "succeeded" and self.result is not None:
            raise ValueError("image availability result requires success")
        return self


class RecipeOperatorRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    with_model: bool = False


class RecipeOperatorResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    action: Literal["remove"]
    selector: str = Field(min_length=1, max_length=256)
    request_key: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "queued", "running", "partial", "succeeded", "failed", "cancelled"]
    progress: OperationProgress
    reclaimed_bytes: int = Field(ge=0)
    preserved: list[str] = Field(default_factory=list, max_length=32)
    next_actions: list[str] = Field(default_factory=list, max_length=32)
    cancelled_operations: list[str] = Field(default_factory=list, max_length=32)
    cancelled_builds: list[str] = Field(default_factory=list, max_length=32)
    model_removals: list[str] = Field(default_factory=list, max_length=32)


class RecipeUpdateRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    selectors: list[str] | None = Field(default=None, max_length=100)
    all: bool = False


class RecipeUpdateResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    action: Literal["update"] = "update"
    updates: list[RecipeImageAvailabilityResponse] = Field(max_length=100)


RecipeOperationResponse = RecipeImageAvailabilityResponse | RecipeOperatorResponse


RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS = {
    ("get", "/api/recipe/operations/{operation_id}"): "getRecipeOperation",
    ("post", "/api/recipe/{selector}/download"): "downloadRecipe",
    ("post", "/api/recipe/{selector}/remove"): "removeRecipe",
    ("post", "/api/recipe/update"): "updateRecipes",
}


def _progress(value: object) -> OperationProgress:
    if not isinstance(value, dict):
        raise TypeError("progress must be a JSON object")
    raw = dict(value)
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
        for key in OperationProgress.model_fields
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
    children = []
    if view.model_child is not None:
        children.append(_child(view.model_child, kind="model-cache"))
    # image_progress is intentionally optional while the runtime image child
    # has not been created. Once present, it is required to be a typed object.
    if view.image_progress is not None:
        children.append(_child({
            "id": view.id,
            "request_key": view.request_id,
            "state": view.image_state or view.state,
            "progress": view.image_progress,
            "failure": view.image_failure,
        }, kind="runtime-image"))
    return RecipeImageAvailabilityResponse(
        id=str(document["id"]), request_id=str(document["request_id"]), kind=str(document["kind"]),
        state=str(document["state"]), attempt=require_integer(document["attempt"], "attempt"),
        recipe_revision_id=str(document["recipe_revision_id"]),
        recipe_content_sha256=str(document["recipe_content_sha256"]),
        progress=_progress(document.get("progress")),
        children=children,
        result=result_model,
        failure=AvailabilityOperationFailure.model_validate(failure) if isinstance(failure, dict) else None,
        actions=[RecipeImageAvailabilityAction(key=str(item)) for item in document.get("supported_actions", [])],
        created_at=str(document["created_at"]), updated_at=str(document["updated_at"]),
    )


def _service(service: RecipeImageAvailabilityService | None) -> RecipeImageAvailabilityService:
    if service is None:
        raise HTTPException(status_code=503, detail="recipe image availability is unavailable")
    return service


def _mutating(actor: Any, route: str) -> None:
    if getattr(actor, "role", None) not in MUTATION_ROLES[("POST", route)]:
        raise HTTPException(status_code=403, detail="insufficient role")


def _recipe_error(error: BaseException) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="recipe operation was not found")
    code = str(getattr(error, "code", ""))
    if code.endswith("selector_missing"):
        return HTTPException(status_code=404, detail=str(error))
    if code.endswith(("selector_ambiguous", "request_key_reused")):
        return HTTPException(status_code=409, detail=str(error))
    if code.endswith("invalid"):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=503, detail="recipe image availability is unavailable")


def install_recipe_operator_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: RecipeImageAvailabilityService | None,
    audits: Any | None = None,
) -> None:
    """Install current singular Recipe download/remove/update mutations."""

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS)

    def removal_document(result: Mapping[str, object]) -> RecipeOperatorResponse:
        return RecipeOperatorResponse(
            schema_version=integer(result.get("schema_version"), default=2),
            action="remove",
            selector=str(result["selector"]),
            request_key=str(result["request_key"]),
            operation_id=str(result["operation_id"]),
            recipe_revision_id=str(result["recipe_revision_id"]),
            state=str(result["state"]),
            progress=OperationProgress(
                phase="completed",
                completed_bytes=integer(result.get("reclaimed_bytes"), default=0),
                total_bytes=integer(result.get("reclaimed_bytes"), default=0),
                total_bytes_known=True,
                completed_items=len(result.get("cancelled_operations", [])),
                total_items=len(result.get("cancelled_operations", [])),
            ),
            reclaimed_bytes=integer(result.get("reclaimed_bytes"), default=0),
            preserved=[str(item) for item in result.get("preserved", [])],
            next_actions=[str(item) for item in result.get("next_actions", [])],
            cancelled_operations=[str(item) for item in result.get("cancelled_operations", [])],
            cancelled_builds=[str(item) for item in result.get("cancelled_builds", [])],
            model_removals=[str(item) for item in result.get("model_removals", [])],
        )

    @app.get(
        "/api/recipe/operations/{operation_id}",
        response_model=RecipeOperationResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getRecipeOperation",
    )
    def get_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=128)],
        actor: Any = actor_dependency,
    ) -> RecipeOperationResponse:
        """Observe a submitted recipe mutation; any authenticated actor may read it."""

        del actor
        try:
            operation = _service(service).get_operator_operation(operation_id)
            if isinstance(operation, RecipeImageAvailabilityView):
                return _view_document(operation)
            return removal_document(operation)
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None

    @app.post(
        "/api/recipe/{selector:path}/download",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecipeImageAvailabilityResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="downloadRecipe",
    )
    def download(
        body: RecipeOperatorRequest,
        _request: Request,
        selector: str = Path(min_length=1, max_length=256),
        actor: Any = actor_dependency,
    ) -> RecipeImageAvailabilityResponse:
        _mutating(actor, "/api/recipe/{selector:path}/download")
        try:
            if service is None:
                raise HTTPException(status_code=503, detail="recipe image availability is unavailable")
            return _view_document(service.start_selector(
                selector,
                actor=actor.subject,
                request_id=body.request_key,
                force=True,
            ))
        except HTTPException:
            raise
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None

    @app.post(
        "/api/recipe/{selector:path}/remove",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecipeOperatorResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="removeRecipe",
    )
    def remove(
        body: RecipeOperatorRequest,
        _request: Request,
        selector: str = Path(min_length=1, max_length=256),
        actor: Any = actor_dependency,
    ) -> RecipeOperatorResponse:
        _mutating(actor, "/api/recipe/{selector:path}/remove")
        try:
            if service is None:
                raise HTTPException(status_code=503, detail="recipe image availability is unavailable")
            result = service.remove_selector(
                selector,
                actor=actor.subject,
                request_id=body.request_key,
                with_model=body.with_model,
            )
            return removal_document(result)
        except HTTPException:
            raise
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None

    @app.post(
        "/api/recipe/update",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecipeUpdateResponse,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="updateRecipes",
    )
    def update(
        body: RecipeUpdateRequest,
        _request: Request,
        actor: Any = actor_dependency,
    ) -> RecipeUpdateResponse:
        _mutating(actor, "/api/recipe/update")
        try:
            if service is None:
                raise HTTPException(status_code=503, detail="recipe image availability is unavailable")
            views = service.update(
                actor=actor.subject,
                request_id=body.request_key,
                selectors=body.selectors,
                all=body.all,
            )
            return RecipeUpdateResponse(updates=[_view_document(view) for view in views])
        except HTTPException:
            raise
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None


__all__ = [
    "RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS",
    "RecipeImageAvailabilityResponse",
    "RecipeOperationResponse",
    "RecipeOperatorRequest",
    "RecipeOperatorResponse",
    "RecipeUpdateRequest",
    "RecipeUpdateResponse",
    "install_recipe_operator_routes",
]
