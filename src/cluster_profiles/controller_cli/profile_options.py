"""Recipe option validation and interactive choices."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Mapping, Sequence

from ..cli_render import terminal_text
from ..cli_select import SelectorError
from ..control_client import ControlMalformedResponse, validate_control_document
from .common import ControllerClient, _quoted
from .selection import _observe_selection, _selection_remaining


def _configure_option_target(
    assignments: list[dict[str, object]], name: str | None
) -> dict[str, object]:
    if name is None:
        if not assignments:
            raise ControlMalformedResponse("option assignment is not yet observed")
        if len(assignments) != 1:
            raise SelectorError(
                "profile configure --option needs --assignment NAME when the "
                "profile does not have exactly one assignment"
            )
        return assignments[0]
    matching = [
        assignment
        for assignment in assignments
        if str(assignment.get("assignment_name", "")).casefold() == name.casefold()
    ]
    if not matching:
        raise ControlMalformedResponse("option assignment is not yet observed")
    if len(matching) != 1:
        raise SelectorError(f"ambiguous assignment named {name}")
    return matching[0]


def _parse_option_flags(values: Sequence[str]) -> dict[str, str]:
    requested: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name or not value or name in requested:
            raise ValueError("--option requires unique NAME=VALUE values")
        requested[name] = value
    return requested


def _option_interactive(args: argparse.Namespace) -> bool:
    return (
        not (
            getattr(args, "global_json", False)
            or getattr(args, "json", False)
            or getattr(args, "no_input", False)
        )
        and sys.stdin.isatty()
        and sys.stderr.isatty()
    )


def _apply_option_choices(
    client: ControllerClient,
    assignment: dict[str, object],
    args: argparse.Namespace,
) -> None:
    """Save an explicit choice for every option the recipe declares.

    Precedence per option: ``--option``, the assignment's saved choice, an
    interactive answer, the recipe default. Nothing is ever required; with
    no ``--option`` and no terminal the Controller fills the defaults.
    """

    requested = _parse_option_flags(getattr(args, "option", []))
    saved = assignment.get("option_choices")
    chosen = dict(saved) if isinstance(saved, Mapping) else {}
    chosen.update(requested)
    if not _option_interactive(args):
        # The owner validates explicit choices against its current recipe.
        # Missing metadata never clears saved intent or substitutes defaults.
        if chosen:
            assignment["option_choices"] = chosen
        return
    selector = str(assignment["recipe_selector"])
    deadline = time.monotonic() + getattr(args, "timeout_seconds", 30)

    from ..generated_control.models.recipe_definition import RecipeDefinition
    from ..generated_control.types import Unset

    def recipe_options():
        detail = client.request(
            "GET",
            f"/api/recipe/{_quoted(selector)}",
            timeout_seconds=_selection_remaining(deadline),
        )
        return RecipeDefinition.from_dict(
            validate_control_document("RecipeDefinition", detail.get("document"))
        )

    document = _observe_selection(recipe_options, deadline=deadline)
    options = [] if isinstance(document.options, Unset) else document.options
    for option in options:
        name = option.name
        if name in chosen:
            continue
        default = next(
            choice.value for choice in option.choices if choice.default is True
        )
        chosen[name] = _prompt_option(option.to_dict(), default)
    if chosen:
        assignment["option_choices"] = chosen


def _prompt_option(option: Mapping[str, object], default: str) -> str:
    choices = [c for c in option["choices"] if isinstance(c, Mapping)]  # type: ignore[union-attr]
    values = [str(c["value"]) for c in choices]
    print(terminal_text(f"{option['label']}: {option['help']}"), file=sys.stderr)
    for choice in choices:
        marker = " (default)" if str(choice["value"]) == default else ""
        print(
            terminal_text(f"  {choice['value']}{marker}: {choice['label']}"),
            file=sys.stderr,
        )
    for _ in range(3):
        print(f"{option['name']} [{default}]: ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline(1024)
        if answer == "":
            return default
        answer = answer.strip()
        if not answer:
            return default
        if answer in values:
            return answer
        print(f"Valid values: {', '.join(values)}", file=sys.stderr)
    raise ValueError("option answer does not name a declared value")
