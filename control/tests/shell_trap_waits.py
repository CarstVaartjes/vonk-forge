"""Reject a foreground ``sleep`` in a deploy script that traps TERM or INT.

A shell runs a trap only after the foreground command it waits on ends, so a
``sleep 30`` under ``trap ... 1 2 15`` defers the shutdown handler by up to 30
seconds, past Docker's grace period. Write ``sleep N & wait $!``: ``wait``
returns the moment a trapped signal arrives.
"""

from __future__ import annotations

import re

KIND = "foreground sleep under a TERM/INT trap; use `sleep N & wait $!`"
_TRAP = re.compile(
    r"^\s*trap\s+(?!-)(?:'[^']*'|\"[^\"]*\"|\S+)\s+"
    r"(?:.*\s)?(?:(?:SIG)?(?:TERM|INT)|2|15)(?:\s|$)"
)
_SLEEP = re.compile(
    r"(?:^|[\s;&|({])sleep\s+[^\s;&|]+(?![^\s;&|])(?!\s*&\s*wait\s+\$!)"
)


def scan_source(source: str) -> list[int]:
    lines = [text.split("#", 1)[0] for text in source.splitlines()]
    if not any(_TRAP.search(text) for text in lines):
        return []
    return [number for number, text in enumerate(lines, 1) if _SLEEP.search(text)]
