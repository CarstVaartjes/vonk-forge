"""Workload invocation and run selection."""

from __future__ import annotations

import argparse
import copy
import sys
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Final, cast

from ..cli_outcome import (
    operation_state,
)
from ..cli_render.common import _text, _words, terminal_text
from ..cli_select import SelectorError
from ..cli_states_generated import PROFILE_NOTHING_TO_CHANGE, SUCCEEDED
from ..control_client.errors import ControlMalformedResponse
from ..control_client.schema import validate_control_document
from ..generated_control.models.fleet_profile_effects import FleetProfileEffects
from ..generated_control.models.fleet_profile_run_effect_action import (
    FleetProfileRunEffectAction,
    check_fleet_profile_run_effect_action,
)
from ..generated_control.types import Unset
from .common import ControllerClient, _profile_number, _request_key
from .confirmation import _require_confirmation
from .profile import _profile
from .profile_authoring import _profile_save, _read_saved_definition, _with_assignment
from .profile_load import ReviewedDraft, _load_profile
from .selection import (
    _library_pages,
    _observe_selection,
    _read_concurrently,
    _recipe_rows,
    _resolve_recipe_selector,
    _resolve_spark_selectors,
    _selection_remaining,
    _skipped_read,
)


def _run(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Run one exact library recipe; the Controller prepares what the load needs.

    The plan shown is the Controller's review of the profile with this recipe
    added, before anything is saved. Declining sends nothing. Consent saves the
    profile (the running fleet is unchanged), then starts the reviewed load; its
    owner decides admission. A profile edited meanwhile, or a changed plan,
    rebuilds the edit on the newest profile and asks again. If the command dies
    between the save and the load, the profile is saved and the fleet unchanged;
    running the same command again converges.
    """
    number = _profile_number(args)
    _require_confirmation(args, "run")
    _request_key(args, factory)
    deadline = time.monotonic() + args.timeout_seconds
    spark_names = list(args.spark)
    names: dict[str, str] = {}

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
        names.update(
            {
                str(node["id"]): str(node["display_name"])
                for node in nodes
                if isinstance(node.get("display_name"), str) and node["display_name"]
            }
        )
        return [str(node["id"]) for node in nodes]

    # The library scan and the fleet roster are independent reads.
    recipe, fleet = _read_concurrently(
        lambda: _resolve_run_recipe(client, args.selector, deadline=deadline),
        _skipped_read
        if spark_names
        else lambda: _observe_selection(roster, deadline=deadline),
    )
    selector = cast(str, recipe.get())
    if not spark_names:
        spark_names = cast(list[str], fleet.get())

    save_args = argparse.Namespace(
        profile_number=number,
        expected_revision=None,
        timeout_seconds=max(0.01, _selection_remaining(deadline)),
    )
    edit_args = argparse.Namespace(
        assignment_name=args.assignment_name,
        model_variant=None,
        desired_state="running",
        option=list(getattr(args, "option", [])),
        no_input=getattr(args, "no_input", False),
        global_json=getattr(args, "global_json", False),
        json=getattr(args, "json", False),
    )
    # The first definition read rides with the selector resolutions.
    recipe_outcome, spark_outcome, first_read = _read_concurrently(
        lambda: _resolve_recipe_selector(client, selector, deadline=deadline),
        lambda: _resolve_spark_selectors(client, spark_names, deadline=deadline),
        lambda: _read_saved_definition(client, args),
    )
    recipe_selector = cast(str, recipe_outcome.get())
    spark_ids = cast(list[str], spark_outcome.get())
    prefetched = [first_read]
    flags = list(edit_args.option)
    flag_names = {flag.partition("=")[0] for flag in flags}
    # Option answers this run was given; they outlive a rebuilt draft.
    answered: dict[str, str] = {}

    def own_choices(definition) -> dict:
        for assignment in definition["assignments"]:
            if (
                assignment["recipe_selector"] == recipe_selector
                and assignment.get("assignment_name") == edit_args.assignment_name
            ):
                return assignment.get("option_choices") or {}
        return {}

    def draft() -> ReviewedDraft | dict:
        """This run's edit on the newest saved profile; nothing is written yet.

        A read that did not complete is returned as its observation instead.
        """
        current = (
            cast(dict, prefetched.pop().get())
            if prefetched
            else _read_saved_definition(client, args)
        )
        if args.observation.status != "complete":
            return current
        base = validate_control_document(
            "FleetProfileDefinition", current["definition"]
        )
        saved_choices = own_choices(base)
        edit_args.option = [
            *flags,
            *(f"{name}={value}" for name, value in answered.items()),
        ]
        definition = _with_assignment(
            copy.deepcopy(base), recipe_selector, spark_ids, edit_args, client
        )
        # Only what this run chose is carried: an option with no saved choice
        # was answered here; a saved choice stays the newest saved value.
        answered.update(
            {
                name: value
                for name, value in own_choices(definition).items()
                if name not in saved_choices and name not in flag_names
            }
        )

        def save() -> None:
            saved = _profile_save(
                save_args,
                client,
                definition,
                current_revision=cast(int, current["revision"]),
            )
            if not (
                getattr(args, "global_json", False) or getattr(args, "json", False)
            ):
                for note in cast(list[str], saved.get("warnings", ())):
                    print(terminal_text(f"Note: {note}"), file=sys.stderr)

        return ReviewedDraft(definition, save)

    application = _load_profile(
        args,
        client,
        factory,
        number,
        question=f"Start {selector} on profile {number} with this reviewed plan?",
        review_when_confirmed=True,
        lead=lambda preview: _review_lead(
            number, [names.get(name, name) for name in spark_names], preview
        ),
        draft=draft,
    )
    # A plan that changes nothing submits nothing: the workload already runs.
    already_running = application.get("next_action") == PROFILE_NOTHING_TO_CHANGE
    application_state = SUCCEEDED if already_running else operation_state(application)
    if application_state != SUCCEEDED:
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


# A run effect keeps the workload or ends it; every action but this one ends it.
class _Keep(StrEnum):
    ACTION = "keep"


# Checked against the generated contract, so a renamed action fails at import.
_KEEP: Final[FleetProfileRunEffectAction] = check_fleet_profile_run_effect_action(
    _Keep.ACTION
)


def _review_lead(number: int, spark_names: list[str], preview: object) -> list[str]:
    """The three facts to read before the plan: where, on what, and what stops."""

    if not isinstance(preview, dict):
        return []
    effects = FleetProfileEffects.from_dict(
        validate_control_document("FleetProfileEffects", preview.get("effects"))
    )
    stops = [run.alias for run in effects.runs if run.action != _KEEP]
    for adopted in effects.adopted if not isinstance(effects.adopted, Unset) else ():
        for stop in adopted.stops if not isinstance(adopted.stops, Unset) else ():
            stops.append(stop.effect.alias)
    stopping = sorted(set(stops))
    return [
        f"Profile edited: {number} ({_text(preview.get('profile_name'))})",
        f"Sparks selected: {_words(spark_names)}",
        f"Will stop: {', '.join(stopping) if stopping else 'nothing'}",
        "",
    ]


def _model_titles(client: ControllerClient, *, deadline: float) -> dict[str, str]:
    """Selector -> published model title, from the paged model library.

    A model without a published display has no title to match and is omitted.
    """
    from ..generated_control.models.model_display import ModelDisplay

    titles: dict[str, str] = {}
    for models in _library_pages(
        client,
        "/api/model/library",
        "models",
        {"sort": "name"},
        deadline=deadline,
    ):
        for row in models:
            if not isinstance(row, Mapping) or not isinstance(row.get("selector"), str):
                raise ControlMalformedResponse(
                    "model library response contains an invalid model"
                )
            display = row.get("model_display")
            if display is not None:
                titles[cast(str, row["selector"])] = ModelDisplay.from_dict(
                    validate_control_document("ModelDisplay", display)
                ).model_title
    return titles


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
            # Human selection uses the Controller's selector projection;
            # model content identity remains with the accepted recipe.
            if needle in {
                model_selector.casefold(),
                model_selector.rsplit("/", 1)[-1].casefold(),
            }:
                model_matches.add(selector)
    if not matches and not model_matches:
        # One paged library read carries every model's published title.
        titles = _observe_selection(
            lambda: _model_titles(client, deadline=deadline), deadline=deadline
        )
        for model_selector, recipes in model_recipes.items():
            if titles.get(model_selector, "").casefold() == needle:
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
