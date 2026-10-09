"""Workload invocation and run selection."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Mapping
from typing import cast

from ..cli_outcome import (
    operation_state,
)
from ..cli_select import SelectorError
from ..control_client import (
    ControlMalformedResponse,
    validate_control_document,
)
from .common import ControllerClient, _profile_number, _quoted, _request_key
from .confirmation import ActionDeclined, _require_confirmation
from .profile import _profile
from .profile_authoring import _profile_authoring
from .profile_load import _load_profile
from .selection import (
    _observe_selection,
    _recipe_rows,
    _resolve_recipe_selector,
    _selection_remaining,
)


def _run(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Run one exact library recipe; the Controller prepares what the load needs.

    Two separate effects follow one invocation. The recipe is first saved into
    the selected profile as a draft (the Controller can only review a saved
    profile; the running fleet is unchanged), then the reviewed load starts it.
    The load follows only a confirmed draft save; its owner decides admission.
    """
    number = _profile_number(args)
    _require_confirmation(args, "run")
    _request_key(args, factory)
    deadline = time.monotonic() + args.timeout_seconds
    selector = _resolve_run_recipe(client, args.selector, deadline=deadline)

    spark_names = list(args.spark)
    if not spark_names:

        def roster():
            fleet = client.request(
                "GET", "/api/fleet", timeout_seconds=_selection_remaining(deadline)
            )
            nodes = fleet.get("nodes")
            if not isinstance(nodes, list) or any(
                not isinstance(node, Mapping) for node in nodes
            ):
                raise ControlMalformedResponse("fleet membership is unavailable")
            if not nodes or any(
                not isinstance(node.get("id"), str) or not node["id"] for node in nodes
            ):
                raise ControlMalformedResponse("fleet membership is not yet observed")
            return [str(node["id"]) for node in nodes]

        spark_names = _observe_selection(roster, deadline=deadline)

    edit_args = argparse.Namespace(
        command="profile",
        profile_number=number,
        profile_action="add",
        recipe_selector=selector,
        spark=spark_names,
        assignment_name=args.assignment_name,
        model_variant=None,
        desired_state="running",
        option=list(getattr(args, "option", [])),
        no_input=getattr(args, "no_input", False),
        global_json=getattr(args, "global_json", False),
        json=getattr(args, "json", False),
        expected_revision=None,
        timeout_seconds=max(0.01, _selection_remaining(deadline)),
    )
    saved = _profile_authoring(edit_args, client)
    if not getattr(edit_args, "profile_saved", False):
        args.observation = getattr(edit_args, "observation", None)
        return saved

    try:
        application = _load_profile(
            args,
            client,
            factory,
            number,
            question=f"Start {selector} on profile {number} with this reviewed plan?",
            review_when_confirmed=True,
        )
    except ActionDeclined as declined:
        raise ActionDeclined(
            f"Not confirmed; the fleet is unchanged. {selector} stays in profile "
            f"{number} as a saved draft.",
        ) from declined
    application_state = operation_state(application)
    if application_state not in {"succeeded", "completed"}:
        return {
            "recipe": selector,
            "application": application,
            "state": application_state or "unknown",
        }
    endpoint_args = argparse.Namespace(
        command="profile", profile_number=number, profile_action="endpoint", alias=None
    )
    endpoints = _profile(endpoint_args, client, factory)
    return {
        "recipe": selector,
        "application": application,
        "state": application_state,
        "endpoints": endpoints,
    }


def _resolve_run_recipe(
    client: ControllerClient, requested: str, *, deadline: float
) -> str:
    """Accept a recipe selector or resolve a model name to one unique recipe."""
    needle = requested.strip().casefold()
    if not needle:
        raise SelectorError("model or recipe name cannot be empty")
    import re

    if re.fullmatch(r"[0-9a-f]{64}|[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", requested):
        return _resolve_recipe_selector(client, requested, deadline=deadline)
    matches: set[str] = set()
    model_matches: set[str] = set()
    model_recipes: dict[str, set[str]] = {}
    for row in _recipe_rows(client, deadline=deadline):
        selector = cast(str, row["selector"])
        identity = cast(Mapping[str, object], row["identity"])
        if any(
            isinstance(value, str) and value.casefold() == needle
            for value in (
                selector,
                identity.get("recipe_id"),
                identity.get("slug"),
                identity.get("title"),
            )
        ):
            matches.add(selector)
        from ..generated_control.models.library_recipe_projection import (
            LibraryRecipeProjection,
        )

        recipe = LibraryRecipeProjection.from_dict(row)
        for model_selector in recipe.model_selectors:
            model_recipes.setdefault(model_selector, set()).add(selector)
        for selected in recipe.document.models:
            model = selected.model
            if needle in {
                model.slug.casefold(),
                f"{model.publisher}/{model.slug}".casefold(),
            }:
                model_matches.add(selector)
    if not matches and not model_matches:
        from ..generated_control.models.model_definition import ModelDefinition

        for model_selector, recipes in model_recipes.items():

            def model_title(selected: str = model_selector):
                detail = client.request(
                    "GET",
                    f"/api/model/{_quoted(selected)}",
                    timeout_seconds=_selection_remaining(deadline),
                )
                if detail.get("selector") != selected:
                    raise ControlMalformedResponse(
                        "model title observation identifies another selection"
                    )
                return ModelDefinition.from_dict(
                    validate_control_document("ModelDefinition", detail.get("document"))
                ).identity.model.title

            title = _observe_selection(model_title, deadline=deadline)
            if title.casefold() == needle:
                model_matches.update(recipes)
    candidates = matches or model_matches
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        return _resolve_recipe_selector(client, requested, deadline=deadline)
    raise SelectorError(
        f"{requested} matches multiple recipes",
        candidates=tuple(sorted(candidates)),
    )
