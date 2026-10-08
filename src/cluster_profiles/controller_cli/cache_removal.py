"""Reviewed cache removal and exact receipt validation."""

from __future__ import annotations

import argparse
import math
import re
import shlex
import sys
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from typing import cast

from ..cli_outcome import (
    Submission,
)
from ..cli_render import render_payload
from ..control_client import (
    ControlClientError,
    ControlMalformedResponse,
    ControlNotFound,
    validate_control_document,
)
from .common import ControllerClient, _quoted, _request_key
from .confirmation import _can_prompt, _confirm_action
from .submission import _submit_idempotent_request


def _cache_operation_id(noun: str, result: Mapping[str, object]) -> str:
    if noun == "model":
        field = "operation_id"
    elif result.get("kind") in {
        "recipe.image.availability.v2",
        "recipe.cache.update.v2",
    }:
        field = "id"
    elif result.get("action") == "remove":
        field = "operation_id"
    else:
        raise ControlMalformedResponse("recipe response does not identify an operation")
    operation_id = result.get(field)
    if not isinstance(operation_id, str) or not operation_id:
        raise ControlMalformedResponse("cache response does not identify an operation")
    return operation_id


def _validate_cache_removal_receipt(
    noun: str,
    selector: str,
    request_key: str,
    result: Mapping[str, object],
    *,
    expected_with_model: bool | None,
) -> str:
    """Bind a removal receipt to its submitted target and durable identity."""

    if noun == "recipe" and result.get("state") == "unknown":
        unavailable = validate_control_document(
            "RecipeRemovalUnavailableView", dict(result)
        )
        if unavailable["request_key"] != request_key:
            raise ControlMalformedResponse("unknown removal identifies another request")
        # This binds observation to the accepted Job; it does not confirm the
        # unreadable target, retention choice, effect or successful outcome.
        return _cache_operation_id(noun, unavailable)
    contract = (
        "ModelCacheOperatorResponse" if noun == "model" else "RecipeOperatorResponse"
    )
    try:
        receipt = validate_control_document(contract, dict(result))
    except ControlClientError:
        raise ControlMalformedResponse(
            f"{noun} removal receipt does not match its canonical contract"
        ) from None
    receipt_selector = receipt.get("selector")
    selector_matches = (
        isinstance(receipt_selector, str)
        and receipt_selector.strip().casefold() == selector.strip().casefold()
    )
    identity_matches = (
        receipt.get("action") == "remove"
        and selector_matches
        and receipt.get("request_key") == request_key
    )
    if noun == "recipe":
        identity_matches = (
            identity_matches
            and expected_with_model is not None
            and receipt.get("with_model") is expected_with_model
        )
    else:
        identity_matches = identity_matches and expected_with_model is None
    if not identity_matches:
        raise ControlMalformedResponse(
            f"{noun} removal receipt identifies another request, selector, or retention choice"
        )
    return _cache_operation_id(noun, receipt)


def _cache_removal_review(
    client: ControllerClient,
    noun: str,
    selector: str,
    *,
    with_model: bool | None,
) -> dict[str, object]:
    path = f"/api/{noun}/{_quoted(selector)}/remove-review"
    query = {"with_model": with_model} if noun == "recipe" else None
    document = client.request("GET", path, query=query)
    try:
        review = validate_control_document("CacheRemovalReview", document)
    except ControlClientError:
        raise ControlMalformedResponse(
            f"{noun} removal review does not match its canonical contract"
        ) from None
    target_identity = review.get("target_identity")
    review_selector = review.get("selector")
    if (
        review.get("action") != "remove"
        or review.get("resource_kind") != noun
        or not isinstance(review_selector, str)
        or review_selector.strip().casefold() != selector.strip().casefold()
        or not isinstance(target_identity, str)
        or not target_identity
    ):
        raise ControlMalformedResponse(
            f"{noun} removal review identifies another selector or resource"
        )
    if noun == "model" and re.fullmatch(r"[0-9a-f]{64}", target_identity) is None:
        raise ControlMalformedResponse("model removal review has no content identity")
    if noun == "model" and (
        "with_model" not in review or review.get("with_model") is not None
    ):
        raise ControlMalformedResponse(
            "model removal review has an invalid model-retention choice"
        )
    if noun == "recipe" and review.get("with_model") is not with_model:
        raise ControlMalformedResponse(
            "recipe removal review identifies another model-retention choice"
        )
    return review


def _confirm_removal(
    client: ControllerClient,
    noun: str,
    selector: str,
    args: argparse.Namespace,
    *,
    with_model: bool | None,
) -> None:
    interactive = _can_prompt(args)
    if not args.yes and not interactive:
        raise ValueError(f"{noun} remove requires --yes in noninteractive mode")

    review = _cache_removal_review(client, noun, selector, with_model=with_model)
    if not args.yes:
        with redirect_stdout(sys.stderr):
            render_payload(review, noun, action="preview")
        _confirm_action(args, f"Remove {noun} selector {selector}?")


def _existing_cache_removal(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    noun: str,
    selector: str,
    with_model: bool | None,
) -> dict[str, object] | None:
    """Reconcile a caller-supplied request before consulting mutable selectors."""
    if getattr(args, "request_key", None) is None:
        return None
    key = _request_key(args, factory)
    path = f"/api/{noun}/{_quoted(selector)}/remove"
    lookup = f"/api/{noun}/requests/{_quoted(key)}"
    timeout = client.request_timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    submission = Submission(
        key,
        path,
        lookup,
        3 * timeout,
        action="remove",
        acceptance="not_submitted",
    )
    args.submission = submission
    try:
        existing = client.request("GET", lookup)
    except ControlNotFound:
        return None
    submission.operation_id = _validate_cache_removal_receipt(
        noun,
        selector,
        key,
        existing,
        expected_with_model=with_model,
    )
    submission.acceptance = "accepted"
    return existing


def _submit_model_removal(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Submit one removal of the model's current state."""
    selector = cast(str, args.selector)
    key = _request_key(args, factory)
    path = f"/api/model/{_quoted(selector)}/remove"
    lookup = f"/api/model/requests/{_quoted(key)}"

    def validate(result: Mapping[str, object]) -> str:
        return _validate_cache_removal_receipt(
            "model",
            selector,
            key,
            result,
            expected_with_model=None,
        )

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body={"request_key": key},
        noun="model",
        action="remove",
        validate=validate,
        lookup_validate=validate,
        reconnect=shlex.join(
            [
                "vonkctl",
                "model",
                "remove",
                selector,
                "--yes",
                "--request-key",
                key,
            ]
        ),
    )


def _submit_recipe_removal(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    with_model: bool,
) -> dict[str, object]:
    """Submit one recipe removal bound to its retention choice."""
    selector = cast(str, args.selector)
    key = _request_key(args, factory)
    path = f"/api/recipe/{_quoted(selector)}/remove"
    lookup = f"/api/recipe/requests/{_quoted(key)}"

    def validate(result: Mapping[str, object]) -> str:
        return _validate_cache_removal_receipt(
            "recipe",
            selector,
            key,
            result,
            expected_with_model=with_model,
        )

    choice = "--with-model" if with_model else "--keep-model"
    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body={
            "request_key": key,
            "with_model": with_model,
        },
        noun="recipe",
        action="remove",
        validate=validate,
        lookup_validate=validate,
        reconnect=shlex.join(
            [
                "vonkctl",
                "recipe",
                "remove",
                selector,
                choice,
                "--yes",
                "--request-key",
                key,
            ]
        ),
    )


def _remove_model(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    selector = args.selector.strip()
    if not selector:
        raise ValueError("model remove requires a non-empty selector")
    args.selector = selector
    if args.review:
        if args.yes or args.request_key is not None:
            raise ValueError(
                "model remove --review cannot be combined with consent or request flags"
            )
        if args.detach:
            raise ValueError("model remove --review cannot be detached")
        args.outcome_context = "read"
        args.model_action = "preview"
        return _cache_removal_review(client, "model", selector, with_model=None)

    existing = _existing_cache_removal(
        client,
        args,
        factory,
        noun="model",
        selector=selector,
        with_model=None,
    )
    if existing is not None:
        return existing
    _confirm_removal(client, "model", selector, args, with_model=None)
    return _submit_model_removal(client, args, factory)


def _remove_recipe(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    selector = args.selector.strip()
    if not selector:
        raise ValueError("recipe remove requires a non-empty selector")
    args.selector = selector
    if not (args.with_model or args.keep_model):
        raise ValueError("recipe remove requires --with-model or --keep-model")
    with_model = args.with_model and not args.keep_model
    if args.review:
        if args.yes or args.request_key is not None:
            raise ValueError(
                "recipe remove --review cannot be combined with consent or request flags"
            )
        if args.detach:
            raise ValueError("recipe remove --review cannot be detached")
        args.outcome_context = "read"
        args.recipe_action = "preview"
        return _cache_removal_review(client, "recipe", selector, with_model=with_model)

    existing = _existing_cache_removal(
        client,
        args,
        factory,
        noun="recipe",
        selector=selector,
        with_model=with_model,
    )
    if existing is not None:
        return existing
    _confirm_removal(client, "recipe", selector, args, with_model=with_model)
    return _submit_recipe_removal(client, args, factory, with_model=with_model)
