"""A Spark's host checks as lines, one rule shared with the Controller web.

The Controller owns every word; the vectors are
``tests/fixtures/fleet-wording-host-checks.json``.
"""

from __future__ import annotations

from cluster_profiles.cli_render.common import _text
from cluster_profiles.generated_control.models.host_check_advice import HostCheckAdvice


def host_check_lines(advice: HostCheckAdvice) -> list[str]:
    """The finding and its explanation, each command, then Undo: and its commands."""
    return [
        f"{_text(advice.finding)} {_text(advice.explanation)}",
        *(_text(command) for command in advice.commands),
        *(
            ["Undo:", *(_text(command) for command in advice.undo)]
            if advice.undo
            else []
        ),
    ]
