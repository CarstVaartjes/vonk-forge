"""Recipe option validation and interactive choices."""

from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Mapping, Sequence

from ..cli_render import terminal_text
from ..cli_select import SelectorError
from .common import ControllerClient, _quoted


def _configure_option_target(
    assignments: list[dict[str, object]], name: str | None
) -> dict[str, object]:
    if name is None:
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
    if len(matching) != 1:
        raise SelectorError(f"no unique assignment named {name}")
    return matching[0]


def _parse_option_flags(values: Sequence[str]) -> dict[str, str]:
    requested: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name or not value or name in requested:
            raise ValueError(
                "--option requires unique NAME=VALUE values. "
                "Next: vonkctl recipe detail <recipe>  (lists the options)"
            )
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
    if not requested and not _option_interactive(args):
        # Nothing was chosen and nobody can be asked: the Controller saves the
        # recipe's defaults for every option on this same save.
        return
    selector = str(assignment["recipe_selector"])
    detail = client.request("GET", f"/api/recipe/{_quoted(selector)}")
    document = detail.get("document")
    declared = (
        document.get("options") if isinstance(document, Mapping) else None
    ) or []
    options = {
        str(option["name"]): option
        for option in declared
        if isinstance(option, Mapping)
    }
    hint = f"Next: vonkctl recipe detail {shlex.quote(selector)}"
    for name, value in requested.items():
        option = options.get(name)
        if option is None:
            raise ValueError(
                f"recipe {selector} has no option {name}; options: "
                f"{', '.join(sorted(options)) or 'none'}. {hint}"
            )
        values = [str(choice["value"]) for choice in option["choices"]]
        if value not in values:
            raise ValueError(
                f"unknown value {value} for option {name}; choices: "
                f"{', '.join(values)}. {hint}"
            )
    saved = assignment.get("option_choices")
    saved = dict(saved) if isinstance(saved, Mapping) else {}
    chosen: dict[str, str] = {}
    for name, option in options.items():
        choices = option["choices"]
        default = next(str(c["value"]) for c in choices if c.get("default"))
        if name in requested:
            chosen[name] = requested[name]
        elif name in saved and any(str(c["value"]) == saved[name] for c in choices):
            chosen[name] = str(saved[name])
        elif _option_interactive(args):
            chosen[name] = _prompt_option(option, default)
        else:
            chosen[name] = default
    if chosen:
        assignment["option_choices"] = chosen
    else:
        assignment.pop("option_choices", None)


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
    while True:
        print(f"{option['name']} [{default}]: ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline(1024)
        if answer == "":
            return default
        answer = answer.strip()
        if not answer:
            return default
        if answer in values:
            return answer
        print(f"Choose one of: {', '.join(values)}", file=sys.stderr)
