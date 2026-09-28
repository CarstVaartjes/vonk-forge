"""Help overviews and usage errors that read like a map of the command tree.

The root and each command group print a short overview: what the group is
for, every command as the full invocation with its arguments, and the next
step. Options belong to the command that takes them, so a group overview lists
none. Leaf commands keep argparse's option help.
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
import textwrap

START_HERE = (
    ("vonkctl fleet", "What every Spark runs and what needs attention"),
    ("vonkctl run <recipe>", "Prepare a recipe and start it"),
    ("vonkctl profile endpoint", "API base and model name for clients"),
    ("vonkctl key create <name>", "A key for an app to call the models"),
    ("vonkctl recipe library", "Recipes you can run"),
)

# The root and the groups whose bare invocation shows their current state.
GROUP_VIEWS = frozenset(
    {
        "vonkctl",
        "vonkctl fleet",
        "vonkctl model",
        "vonkctl recipe",
        "vonkctl profile",
        "vonkctl key",
    }
)


def subcommands(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # type: ignore[type-arg]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def invocation(prog: str, parser: argparse.ArgumentParser) -> str:
    """Return the command with the arguments it cannot run without."""

    parts = [prog]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            parts.append("[command]" if prog in GROUP_VIEWS else "<command>")
        elif not action.option_strings:
            name = (
                "<" + str(action.metavar or action.dest).lower().replace("_", "-") + ">"
            )
            parts.append(f"[{name}]" if action.nargs == "?" else name)
        elif action.required:
            option = action.option_strings[-1]
            value = str(action.metavar or action.dest).lower().replace("_", "-")
            parts.append(option if action.nargs == 0 else f"{option} <{value}>")
    return " ".join(parts)


def _rows(rows: list[tuple[str, str]]) -> list[str]:
    columns = max(40, min(shutil.get_terminal_size((100, 24)).columns, 100))
    width = min(max(len(name) for name, _ in rows), 44)
    lines: list[str] = []
    for name, summary in rows:
        wrapped = textwrap.wrap(summary, max(20, columns - width - 4)) or [""]
        if len(name) > width:
            lines.append(f"  {name}")
            lines.extend(f"  {'':{width}}  {line}" for line in wrapped)
            continue
        lines.append(f"  {name:{width}}  {wrapped[0]}".rstrip())
        lines.extend(f"  {'':{width}}  {line}" for line in wrapped[1:])
    return lines


def group_help(parser: argparse.ArgumentParser) -> str:
    """Render the overview of the root or of one command group."""

    commands = subcommands(parser)
    assert commands is not None
    root = " " not in parser.prog
    lines = [invocation(parser.prog, parser) + (" [flags]" if root else ""), ""]
    if parser.description:
        lines += [*textwrap.wrap(parser.description, 78), ""]
    if root:
        lines += ["Start here", *_rows(list(START_HERE)), ""]
    rows = []
    for choice in commands._choices_actions:
        child = commands.choices[choice.dest]
        name = f"{parser.prog} {choice.dest}"
        rows.append((name if root else invocation(name, child), choice.help or ""))
    lines += ["Commands", *_rows(rows), ""]
    if root:
        flags = [
            (", ".join(action.option_strings) + _value(action), action.help or "")
            for action in parser._actions
            if action.option_strings and action.help != argparse.SUPPRESS
        ]
        lines += ["Flags", *_rows(flags), ""]
    if parser.epilog:
        lines += [*textwrap.wrap(parser.epilog, 78), ""]
    lines.append(f"Run '{parser.prog} <command> --help' for a command's options.")
    return "\n".join(lines) + "\n"


def _value(action: argparse.Action) -> str:
    if action.nargs == 0:
        return ""
    if action.choices is not None and action.metavar is None:
        return " <" + "|".join(map(str, action.choices)) + ">"
    return " <" + str(action.metavar or action.dest).lower().replace("_", "-") + ">"


# Flags every command shares; listed apart from the command's own options.
_SHARED_FLAGS = frozenset({"-h", "--help", "--json", "--wide", "--no-input"})


def _flag_summary(action: argparse.Action) -> str:
    summary = action.help if isinstance(action.help, str) else ""
    extra = []
    default = action.default
    if (
        default not in (None, False, [], "", argparse.SUPPRESS)
        and action.nargs != 0
        and "default" not in summary
    ):
        extra.append(f"default: {default}")
    if isinstance(action, argparse._AppendAction) and "repeat" not in summary:
        extra.append("repeatable")
    if extra:
        summary = f"{summary} ({'; '.join(extra)})".strip()
    return summary


def command_help(parser: argparse.ArgumentParser) -> str:
    """Render one command: how to call it, what it does, and its options."""

    lines = [invocation(parser.prog, parser) + " [flags]", ""]
    if parser.description:
        lines += [*textwrap.wrap(parser.description, 78), ""]
    arguments = [
        (
            _value(action).strip(),
            action.help if isinstance(action.help, str) else "",
        )
        for action in parser._actions
        if not action.option_strings
    ]
    if arguments:
        lines += ["Arguments", *_rows(arguments), ""]
    own = [
        action
        for action in parser._actions
        if action.option_strings
        and action.help != argparse.SUPPRESS
        and not set(action.option_strings) & _SHARED_FLAGS
    ]
    if own:
        rows = [
            (", ".join(action.option_strings) + _value(action), _flag_summary(action))
            for action in own
        ]
        lines += ["Flags", *_rows(rows), ""]
    shared = [
        action.option_strings[-1]
        for action in parser._actions
        if set(action.option_strings) & _SHARED_FLAGS
    ]
    lines.append("Also: " + ", ".join(shared))
    if parser.epilog:
        lines += ["", *textwrap.wrap(parser.epilog, 78)]
    return "\n".join(lines) + "\n"


def usage_error(
    parser: argparse.ArgumentParser, message: str
) -> tuple[str, str | None, str]:
    """Return a short error, the usage line when it helps, and the next step."""

    commands = subcommands(parser)
    help_step = f"{parser.prog} --help"
    unknown = re.search(r"invalid choice: '([^']*)'", message)
    if unknown is not None and commands is not None and "argument" in message:
        word = unknown.group(1)
        text = f"unknown command '{word}' for '{parser.prog}'"
        match = difflib.get_close_matches(word, list(commands.choices), n=1)
        if match:
            text += f". Did you mean '{match[0]}'?"
        return text, None, help_step
    missing = re.match(r"the following arguments are required: (.*)", message)
    if missing is not None:
        names = ", ".join(
            name if name.startswith("-") else f"<{name.lower().replace('_', '-')}>"
            for name in missing.group(1).split(", ")
        )
        return (
            f"'{parser.prog}' needs {names}",
            invocation(parser.prog, parser),
            help_step,
        )
    return message, None, help_step
