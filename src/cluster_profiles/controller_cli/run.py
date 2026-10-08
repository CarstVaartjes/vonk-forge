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
)
from .common import ControllerClient, _profile_number, _request_key
from .confirmation import ActionDeclined, _require_confirmation
from .profile import _profile
from .profile_authoring import _profile_authoring
from .profile_load import _load_profile
from .selection import _recipe_rows, _selection_remaining


def _run(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Run one exact library recipe; the Controller prepares what the load needs.

    Two separate effects follow one invocation. The recipe is first saved into
    the selected profile as a draft (the Controller can only review a saved
    profile; the running fleet is unchanged), then the reviewed load starts it.
    Everything that can be refused is refused before the first effect.
    """
    number = _profile_number(args)
    _require_confirmation(args, "run")
    _request_key(args, factory)
    deadline = time.monotonic() + args.timeout_seconds
    selector = _resolve_run_recipe(client, args.selector, deadline=deadline)

    spark_names = list(args.spark)
    if not spark_names:
        fleet = client.request(
            "GET", "/api/fleet", timeout_seconds=_selection_remaining(deadline)
        )
        nodes = fleet.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(
                "no Sparks are enrolled; enroll a Spark before running a model"
            )
        spark_names = [
            str(node.get("display_name") or node.get("id"))
            for node in nodes
            if isinstance(node, Mapping)
        ]
        if len(spark_names) != len(nodes):
            raise ControlMalformedResponse("fleet response contains an invalid Spark")

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
    _profile_authoring(edit_args, client)

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
            next_steps=(
                f"vonkctl --profile {number} profile load  (load the saved profile)",
                f"vonkctl --profile {number} profile  (review or edit the saved profile)",
            ),
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
    matches: set[str] = set()
    model_matches: set[str] = set()
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
        models = row.get("models", [])
        if isinstance(models, list):
            for selected in models:
                if not isinstance(selected, Mapping):
                    continue
                document = selected.get("model_document")
                model_identity = (
                    document.get("identity") if isinstance(document, Mapping) else None
                )
                if isinstance(model_identity, Mapping) and any(
                    isinstance(value, str) and value.casefold() == needle
                    for value in (
                        model_identity.get("slug"),
                        model_identity.get("title"),
                        f"{model_identity.get('publisher')}/{model_identity.get('slug')}",
                    )
                ):
                    model_matches.add(selector)
    candidates = matches or model_matches
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        raise SelectorError(
            f"no runnable catalog recipe matches {requested}; choose a recipe from `vonkctl recipe library`"
        )
    raise SelectorError(
        f"{requested} matches multiple recipes; choose one exact recipe selector",
        candidates=tuple(sorted(candidates)),
    )
