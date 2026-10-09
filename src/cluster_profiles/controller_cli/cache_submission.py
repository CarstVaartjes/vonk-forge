"""Cache preparation, retry, and cancellation submissions."""

from __future__ import annotations

import argparse
import shlex
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ..cli_states import (
    CANCEL_ACCEPTED_STATES,
)
from ..control_client import (
    ControlClientError,
    ControlMalformedResponse,
    validate_control_document,
)

if TYPE_CHECKING:
    from ..generated_control.models.recipe_image_availability_response import (
        RecipeImageAvailabilityResponse,
    )


from .cache_removal import _cache_operation_id
from .common import ControllerClient, _quoted, _request_key
from .confirmation import _confirm_action
from .submission import _submit_idempotent_request


def _submit_cache_request(
    client: ControllerClient,
    noun: str,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """One POST, exact request reconciliation, and at most one identical replay."""

    key = _request_key(args, factory)
    action = getattr(args, f"{noun}_action")
    if action == "update":
        path = "/api/recipe/update"
    else:
        path = f"/api/{noun}/{_quoted(args.selector)}/download"
    lookup = f"/api/{noun}/requests/{key}"
    body: dict[str, object] = {"request_key": key}
    if action == "update":
        body.update(all=args.all, selectors=[args.selector] if args.selector else [])

    def validate(result: Mapping[str, object]) -> str:
        if action == "update":
            intent = result.get("request")
            matches = (
                result.get("kind") == "recipe.cache.update.v2"
                and result.get("request_id") == key
                and isinstance(intent, Mapping)
                and intent.get("all") is args.all
                and intent.get("selectors") == body["selectors"]
            )
        elif noun == "model":
            returned_selector = result.get("selector")
            matches = (
                result.get("request_key") == key
                and result.get("action") == "download"
                and isinstance(returned_selector, str)
                and returned_selector.strip() == args.selector.strip()
            )
        else:
            intent = result.get("request")
            matches = (
                result.get("kind") == "recipe.image.availability.v2"
                and result.get("request_id") == key
                and isinstance(intent, Mapping)
                and intent.get("kind") == "selector"
                and intent.get("selector") == args.selector
                and intent.get("force") is False
            )
        if not matches:
            raise ControlMalformedResponse(
                f"{action} receipt identifies another request or intent"
            )
        return _cache_operation_id(noun, result)

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun=noun,
        action=action,
        validate=validate,
        reconnect=shlex.join(
            ["vonkctl", noun, "progress", "--request-key", key, "--follow"]
        ),
    )


def _submit_model_cancellation(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Cancel one operation through its durable identity and one safe replay."""

    if not args.yes:
        raise ValueError("model cancel requires --yes in noninteractive mode")
    operation_id = str(uuid.UUID(args.operation_id))
    key = _request_key(args, factory)
    reason = args.reason.strip()
    if not reason or len(reason) > 512:
        raise ValueError("--reason must contain between 1 and 512 characters")
    args.reason = reason
    path = f"/api/model/operations/{_quoted(operation_id)}/cancel"
    lookup = f"/api/model/operations/{_quoted(operation_id)}"
    body: dict[str, object] = {
        "request_key": key,
        "reason": reason,
    }

    def validate(result: Mapping[str, object]) -> str:
        if _cache_operation_id("model", result) != operation_id:
            raise ControlMalformedResponse(
                "model cancellation receipt identifies another operation"
            )
        cancellation = result.get("cancellation")
        if (
            not isinstance(cancellation, Mapping)
            or cancellation.get("request_key") != key
            or cancellation.get("reason") != reason
            or result.get("state") not in CANCEL_ACCEPTED_STATES
        ):
            raise ControlMalformedResponse(
                "model cancellation receipt identifies another cancellation"
            )
        return operation_id

    def validate_existing_cancellation(observed: Mapping[str, object]) -> str:
        if _cache_operation_id("model", observed) != operation_id:
            raise ControlMalformedResponse(
                "model cancellation lookup identifies another operation"
            )
        cancellation = observed.get("cancellation")
        if cancellation is None:
            raise ControlMalformedResponse(
                "cancellation acceptance is not yet observed"
            )
        if not isinstance(cancellation, Mapping):
            raise ControlMalformedResponse("model cancellation lookup is malformed")
        if (
            cancellation.get("request_key") != key
            or cancellation.get("reason") != reason
        ):
            raise ControlMalformedResponse(
                "this cancellation acceptance is not yet observed"
            )
        return validate(observed)

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun="model",
        action="cancel",
        validate=validate,
        lookup_validate=validate_existing_cancellation,
        reconnect=shlex.join(
            [
                "vonkctl",
                "model",
                "cancel",
                operation_id,
                "--yes",
                "--request-key",
                key,
                "--reason",
                reason,
            ]
        ),
    )


def _submit_recipe_retry(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> RecipeImageAvailabilityResponse:
    """Preserve the owning frozen intent instead of a mutable recipe selector."""
    from ..generated_control.models.recipe_image_availability_response import (
        RecipeImageAvailabilityResponse,
    )
    from ..generated_control.models.recipe_retry_intent import RecipeRetryIntent

    def decode(value: object) -> RecipeImageAvailabilityResponse:
        if not isinstance(value, Mapping):
            raise ControlMalformedResponse(
                "recipe preparation receipt is not an object"
            )
        try:
            return RecipeImageAvailabilityResponse.from_dict(
                validate_control_document("RecipeImageAvailabilityResponse", value)
            )
        except (ControlClientError, KeyError, TypeError, ValueError) as error:
            raise ControlMalformedResponse(
                "recipe preparation receipt is malformed"
            ) from error

    original_id = args.operation_id
    _confirm_action(
        args,
        f"Submit recipe preparation from the Controller-owned intent of {original_id}?",
    )
    key = _request_key(args, factory)

    def validate(document: object) -> str:
        result = decode(document)
        intent = result.request
        if (
            result.kind != "recipe.image.availability.v2"
            or result.request_id != key
            or not isinstance(intent, RecipeRetryIntent)
            or intent.kind != "retry"
            or intent.operation_id != original_id
        ):
            raise ControlMalformedResponse(
                "recipe retry receipt identifies another request or frozen intent"
            )
        operation_id = _cache_operation_id("recipe", result.to_dict())
        return operation_id

    return decode(
        _submit_idempotent_request(
            client,
            args,
            key=key,
            path=f"/api/recipe/operations/{_quoted(original_id)}/retry",
            lookup=f"/api/recipe/requests/{key}",
            body={"request_key": key},
            noun="recipe",
            action="retry",
            validate=validate,
            reconnect=shlex.join(
                ["vonkctl", "recipe", "progress", "--request-key", key, "--follow"]
            ),
        )
    )


def _submit_recipe_cancellation(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Cancel one operation through its durable identity and one safe replay."""

    if not args.yes:
        raise ValueError("recipe cancel requires --yes in noninteractive mode")
    operation_id = args.operation_id
    key = _request_key(args, factory)
    reason = " ".join(args.reason.split())
    if not reason or len(reason) > 512:
        raise ValueError("--reason must contain between 1 and 512 characters")
    args.reason = reason
    path = f"/api/recipe/operations/{_quoted(operation_id)}/cancel"
    lookup = f"/api/recipe/operations/{_quoted(operation_id)}"
    body: dict[str, object] = {
        "request_key": key,
        "reason": reason,
    }

    def validate(result: Mapping[str, object]) -> str:
        if _cache_operation_id("recipe", result) != operation_id:
            raise ControlMalformedResponse(
                "recipe cancellation receipt identifies another operation"
            )
        cancellation = result.get("cancellation")
        if (
            not isinstance(cancellation, Mapping)
            or cancellation.get("cancel_request_id") != key
            or cancellation.get("reason") != reason
            or result.get("state") not in CANCEL_ACCEPTED_STATES
        ):
            raise ControlMalformedResponse(
                "recipe cancellation receipt identifies another cancellation"
            )
        return operation_id

    def validate_existing_cancellation(observed: Mapping[str, object]) -> str:
        if _cache_operation_id("recipe", observed) != operation_id:
            raise ControlMalformedResponse(
                "recipe cancellation lookup identifies another operation"
            )
        cancellation = observed.get("cancellation")
        if cancellation is None:
            raise ControlMalformedResponse(
                "cancellation acceptance is not yet observed"
            )
        if not isinstance(cancellation, Mapping):
            raise ControlMalformedResponse("recipe cancellation lookup is malformed")
        if (
            cancellation.get("cancel_request_id") != key
            or cancellation.get("reason") != reason
        ):
            raise ControlMalformedResponse(
                "this cancellation acceptance is not yet observed"
            )
        return validate(observed)

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun="recipe",
        action="cancel",
        validate=validate,
        lookup_validate=validate_existing_cancellation,
        reconnect=shlex.join(
            [
                "vonkctl",
                "recipe",
                "cancel",
                operation_id,
                "--yes",
                "--request-key",
                key,
                "--reason",
                reason,
            ]
        ),
    )
