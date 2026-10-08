"""Interactive consent and typed action declines."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ..cli_render import terminal_text


class ActionDeclined(ValueError):
    """The operator answered no, or gave no answer; the action was not taken."""

    def __init__(self, message: str, *, next_steps: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.next_steps = tuple(next_steps)


def _can_prompt(args: argparse.Namespace) -> bool:
    """True when this invocation can ask the operator a question."""

    return (
        not (
            getattr(args, "no_input", False)
            or getattr(args, "global_json", False)
            or getattr(args, "json", False)
        )
        and sys.stdin.isatty()
        and sys.stderr.isatty()
    )


def _require_confirmation(args: argparse.Namespace, subject: str) -> None:
    """Refuse before any effect when nobody can confirm this invocation."""

    if not args.yes and not _can_prompt(args):
        raise ValueError(f"{subject} requires --yes in noninteractive mode")


def _confirm_action(args: argparse.Namespace, message: str) -> None:
    if args.yes:
        if not (getattr(args, "global_json", False) or getattr(args, "json", False)):
            print(terminal_text(message), file=sys.stderr)
        return
    if not _can_prompt(args):
        raise ValueError(f"{message} Pass --yes to confirm in noninteractive mode")
    print(terminal_text(message) + " [y/N] ", end="", file=sys.stderr, flush=True)
    if sys.stdin.readline(1024).strip().casefold() not in {"y", "yes"}:
        raise ActionDeclined("Not confirmed; nothing was changed.")
