"""Authenticated, typed HTTP routes for Recipe Make available."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Request, status
from pydantic import ConfigDict, Field, field_validator, model_validator

from .auth import MUTATION_ROLES
from .bounded_json import integer, require_integer, require_sequence
from .logging import redact_text
from .model_cache_contract import UUID_PATTERN, Digest
from .operation_api import bounded_error_responses
from .operation_contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
    OperationProgress,
)
from .recipe_availability_intent import RecipeAvailabilityIntent
from .recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    RecipeImageAvailabilityView,
)
from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .recipe_update_contract import RecipeUpdateRequest, RecipeUpdateResponse
from .strict_json import StrictJSONModel

# One named type per closed set, shared by the contract field and every
# helper that produces the value, so the vocabulary cannot drift apart.
RecipeImageAvailabilityKind = Literal["recipe.image.availability.v2"]
RecipeImageAvailabilityState = Literal[
    "queued", "running", "partial", "cancelling", "succeeded", "failed", "cancelled"
]
RecipeImageAvailabilityChildKind = Literal["model-cache", "runtime-image"]
RecipeOperatorState = Literal[
    "accepted", "queued", "running", "partial", "succeeded", "failed", "cancelled"
]
RecipeOperatorAction = Literal["remove"]


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

    kind: RecipeImageAvailabilityChildKind
    id: str
    request_key: str | None = None
    state: RecipeImageAvailabilityState
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
    request: RecipeAvailabilityIntent
    kind: RecipeImageAvailabilityKind
    state: RecipeImageAvailabilityState
    attempt: int = Field(ge=0)
    recipe_revision_id: str
    recipe_content_sha256: str
    progress: OperationProgress
    children: list[RecipeImageAvailabilityChild] = Field(default_factory=list)
    result: RecipeImageAvailabilityResult | None = None
    failure: AvailabilityOperationFailure | None = None
    cancellation: RecipeOperationCancellationResult | None = None
    actions: list[RecipeImageAvailabilityAction] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def terminal_evidence_is_consistent(self) -> RecipeImageAvailabilityResponse:
        if self.state == "succeeded" and (
            self.result is None or self.failure is not None
        ):
            raise ValueError(
                "successful image availability requires a result and no failure"
            )
        if self.state == "failed" and self.failure is None:
            raise ValueError("failed image availability requires failure evidence")
        if self.state != "succeeded" and self.result is not None:
            raise ValueError("image availability result requires success")
        return self


class RecipeDownloadRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    request_key: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )


class RecipeOperatorRequest(RecipeDownloadRequest):
    with_model: bool = False


class RecipeCancellationRequest(RecipeDownloadRequest):
    reason: str = Field(min_length=1, max_length=512)


class RecipeOperatorResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    action: RecipeOperatorAction
    selector: str = Field(min_length=1, max_length=256)
    request_key: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    with_model: bool
    state: RecipeOperatorState
    progress: OperationProgress
    reclaimed_bytes: int = Field(ge=0)
    preserved: list[str] = Field(default_factory=list, max_length=32)
    next_actions: list[str] = Field(default_factory=list, max_length=32)
    cancelled_operations: list[str] = Field(default_factory=list, max_length=32)
    cancelled_builds: list[str] = Field(default_factory=list, max_length=32)
    model_removals: list[str] = Field(default_factory=list, max_length=32)
    failure: AvailabilityOperationFailure | None = None

    @model_validator(mode="after")
    def terminal_removal_evidence_is_consistent(self) -> RecipeOperatorResponse:
        if self.state == "succeeded":
            if self.failure is not None or self.progress.phase != "completed":
                raise ValueError(
                    "successful recipe removal requires completed progress and no failure"
                )
        elif self.progress.phase == "completed":
            raise ValueError(
                "unfinished recipe removal cannot report completed progress"
            )
        if self.state == "failed" and self.failure is None:
            raise ValueError("failed recipe removal requires failure evidence")
        return self


RecipeOperationResponse = (
    RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse
)


RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS = {
    ("get", "/api/recipe/operations/{operation_id}"): "getRecipeOperation",
    ("get", "/api/recipe/requests/{request_key}"): "getRecipeRequest",
    ("post", "/api/recipe/operations/{operation_id}/cancel"): "cancelRecipeOperation",
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
    return OperationProgress.model_validate(
        {key: raw[key] for key in OperationProgress.model_fields if key in raw}
    )


def _child(
    value: object, *, kind: RecipeImageAvailabilityChildKind
) -> RecipeImageAvailabilityChild:
    raw = dict(value) if isinstance(value, dict) else {}
    if kind == "runtime-image":
        raw["model_content_digests"] = []
    return RecipeImageAvailabilityChild.model_validate(
        raw | {"kind": kind, "progress": _progress(raw.get("progress"))}
    )


def _view_document(
    view: RecipeImageAvailabilityView,
) -> RecipeImageAvailabilityResponse:
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
                "artifact_set_sha256": child.get("artifact_set_sha256")
                if isinstance(child, dict)
                else None,
                "model_content_digests": (
                    child["model_content_digests"] if isinstance(child, dict) else []
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
        children.append(
            _child(
                {
                    "id": view.id,
                    "request_key": view.request_id,
                    "state": view.image_state or view.state,
                    "progress": view.image_progress,
                    "failure": view.image_failure,
                },
                kind="runtime-image",
            )
        )
    return RecipeImageAvailabilityResponse.model_validate(
        {
            "id": str(document["id"]),
            "request_id": str(document["request_id"]),
            "request": view.request,
            "kind": document["kind"],
            "state": document["state"],
            "attempt": require_integer(document["attempt"], "attempt"),
            "recipe_revision_id": str(document["recipe_revision_id"]),
            "recipe_content_sha256": str(document["recipe_content_sha256"]),
            "progress": _progress(document.get("progress")),
            "children": children,
            "result": result_model,
            "failure": (
                AvailabilityOperationFailure.model_validate(failure)
                if isinstance(failure, dict)
                else None
            ),
            "cancellation": view.cancellation,
            "actions": [
                RecipeImageAvailabilityAction.model_validate({"key": item})
                for item in require_sequence(
                    document.get("supported_actions", []), "supported actions"
                )
            ],
            "created_at": str(document["created_at"]),
            "updated_at": str(document["updated_at"]),
        }
    )


def _service(
    service: RecipeImageAvailabilityService | None,
) -> RecipeImageAvailabilityService:
    if service is None:
        raise HTTPException(
            status_code=503, detail="recipe image availability is unavailable"
        )
    return service


def _mutating(actor: Any, route: str) -> None:
    if getattr(actor, "role", None) not in MUTATION_ROLES[("POST", route)]:
        raise HTTPException(status_code=403, detail="insufficient role")


# One refusal leaves the Controller as evidence, so its detail names the stable
# code and an operator-facing message instead of a sentence that identifies
# nothing.  The bound matches the one ``validation_detail`` puts on a single
# contract error; the redactor runs first so a secret cannot survive truncation
# in the middle of a token.
_MAX_REFUSAL_DETAIL = 80


def _refusal_detail(error: BaseException) -> str:
    """Name one availability refusal, bounded and redacted.

    Every failure outside the four special cases used to answer with the same
    sentence, so the documented ``recipe download`` prepare-cache recovery step
    left an operator with nothing to inspect and no durable operation record to
    read.  The typed error already carries a stable code and an operator-facing
    message; render both and never echo a stored document, a token, or an
    unbounded value.
    """

    code = redact_text(str(getattr(error, "code", "") or type(error).__name__))
    message = getattr(error, "detail", None)
    if not isinstance(message, str) or not message.strip():
        message = str(error)
    named = code[:_MAX_REFUSAL_DETAIL]
    message = redact_text(message)[:_MAX_REFUSAL_DETAIL]
    return f"{named}: {message}" if message else named


def _recipe_error(error: BaseException) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="recipe operation was not found")
    code = str(getattr(error, "code", ""))
    if code.endswith("selector_missing"):
        return HTTPException(status_code=404, detail=str(error))
    if code.endswith("authority_denied"):
        return HTTPException(status_code=403, detail=str(error))
    if code.endswith(("selector_ambiguous", "request_key_reused", "not_cancellable")):
        return HTTPException(status_code=409, detail=str(error))
    if code.endswith("invalid"):
        return HTTPException(status_code=422, detail=str(error))
    # A typed refusal outside those families is either a transient dependency
    # failure that a later identical request can clear, or a terminal condition
    # (stale metadata, a changed execution identity, an exhausted retry budget,
    # an unrecoverable integrity failure) that it cannot.  The typed error
    # already decided which one it is, so keep 503 Service Unavailable for the
    # former and 409 Conflict for the latter: a client may retry the first and
    # must not loop on the second.
    if isinstance(error, RecipeImageAvailabilityError) and not error.retryable:
        return HTTPException(status_code=409, detail=_refusal_detail(error))
    return HTTPException(status_code=503, detail=_refusal_detail(error))


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
        with_model = result.get("with_model")
        if not isinstance(with_model, bool):
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "stored removal choice is malformed",
            )
        reclaimed_bytes = require_integer(
            result.get("reclaimed_bytes"), "reclaimed bytes"
        )
        cancelled_operations = require_sequence(
            result.get("cancelled_operations", []), "cancelled operations"
        )
        progress = _progress(result.get("progress"))
        failure_value = result.get("failure")
        failure = (
            None
            if failure_value is None
            else AvailabilityOperationFailure.model_validate(failure_value)
        )
        return RecipeOperatorResponse.model_validate(
            {
                "schema_version": integer(result.get("schema_version"), default=2),
                "action": "remove",
                "selector": str(result["selector"]),
                "request_key": str(result["request_key"]),
                "operation_id": str(result["operation_id"]),
                "recipe_revision_id": str(result["recipe_revision_id"]),
                "with_model": with_model,
                "state": result["state"],
                "progress": progress,
                "reclaimed_bytes": reclaimed_bytes,
                "preserved": [
                    str(item)
                    for item in require_sequence(
                        result.get("preserved", []), "preserved"
                    )
                ],
                "next_actions": [
                    str(item)
                    for item in require_sequence(
                        result.get("next_actions", []), "next actions"
                    )
                ],
                "cancelled_operations": [str(item) for item in cancelled_operations],
                "cancelled_builds": [
                    str(item)
                    for item in require_sequence(
                        result.get("cancelled_builds", []), "cancelled builds"
                    )
                ],
                "model_removals": [
                    str(item)
                    for item in require_sequence(
                        result.get("model_removals", []), "model removals"
                    )
                ],
                "failure": failure,
            }
        )

    @app.get(
        "/api/recipe/requests/{request_key}",
        response_model=RecipeOperationResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getRecipeRequest",
    )
    def get_request(
        request_key: Annotated[str, Path(pattern=UUID_PATTERN)],
        actor: Any = actor_dependency,
    ) -> RecipeOperationResponse:
        try:
            operation = _service(service).get_operator_request(
                request_key, actor=actor.subject
            )
            if isinstance(operation, RecipeUpdateResponse):
                return operation
            if isinstance(operation, RecipeImageAvailabilityView):
                return _view_document(operation)
            return removal_document(operation)
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None

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
            if isinstance(operation, RecipeUpdateResponse):
                return operation
            if isinstance(operation, RecipeImageAvailabilityView):
                return _view_document(operation)
            return removal_document(operation)
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None

    @app.post(
        "/api/recipe/operations/{operation_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecipeOperationResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="cancelRecipeOperation",
    )
    def cancel_operation(
        body: RecipeCancellationRequest,
        operation_id: Annotated[str, Path(min_length=1, max_length=128)],
        actor: Any = actor_dependency,
    ) -> RecipeOperationResponse:
        _mutating(actor, "/api/recipe/operations/{operation_id}/cancel")
        try:
            operation = _service(service).cancel(
                operation_id,
                actor=actor.subject,
                request_id=body.request_key,
                reason=body.reason,
            )
            if isinstance(operation, RecipeUpdateResponse):
                return operation
            return _view_document(operation)
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
        body: RecipeDownloadRequest,
        _request: Request,
        selector: str = Path(min_length=1, max_length=256),
        actor: Any = actor_dependency,
    ) -> RecipeImageAvailabilityResponse:
        _mutating(actor, "/api/recipe/{selector:path}/download")
        try:
            if service is None:
                raise HTTPException(
                    status_code=503, detail="recipe image availability is unavailable"
                )
            return _view_document(
                service.start_selector(
                    selector,
                    actor=actor.subject,
                    request_id=body.request_key,
                    force=True,
                )
            )
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
                raise HTTPException(
                    status_code=503, detail="recipe image availability is unavailable"
                )
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
                raise HTTPException(
                    status_code=503, detail="recipe image availability is unavailable"
                )
            return service.update(
                actor=actor.subject,
                request_id=body.request_key,
                selectors=body.selectors,
                all=body.all,
            )
        except HTTPException:
            raise
        except (RecipeImageAvailabilityError, KeyError, ValueError) as error:
            raise _recipe_error(error) from None


__all__ = [
    "RECIPE_IMAGE_AVAILABILITY_OPERATION_IDS",
    "RecipeDownloadRequest",
    "RecipeImageAvailabilityResponse",
    "RecipeOperationResponse",
    "RecipeOperatorRequest",
    "RecipeOperatorResponse",
    "RecipeUpdateRequest",
    "RecipeUpdateResponse",
    "install_recipe_operator_routes",
]
