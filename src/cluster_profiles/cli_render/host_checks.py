"""A Spark's host checks as lines, one rule shared with the Controller web.

The Controller owns every word; the vectors are
``tests/fixtures/fleet-wording-host-checks.json``.
"""

from __future__ import annotations

from ..generated_control.models.host_check_advice import HostCheckAdvice
from .common import _text


def host_check_lines(advice: HostCheckAdvice) -> list[str]:
    """The finding and its explanation, then each command on its own line."""
    return [
        f"{_text(advice.finding)} {_text(advice.explanation)}",
        *(_text(command) for command in advice.commands),
    ]
