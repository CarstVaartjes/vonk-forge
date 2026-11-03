"""Reject a foreground ``sleep`` in a deploy script that traps TERM or INT.

A shell runs a trap only after the foreground command it waits on ends, so a
``sleep 30`` under ``trap ... 1 2 15`` defers the shutdown handler by up to 30
seconds, past Docker's grace period. Background it and ``wait`` for it
(``sleep N & wait $!``, ``sleep N & pid=$!; wait "$pid"``, or ``sleep N &`` with
the ``wait`` on one of the next two lines): ``wait`` returns the moment a
trapped signal arrives. Only ``sleep`` is scanned; other foreground waits
(``timeout``, ``curl``, ``read -t``) are not.
"""

from __future__ import annotations

import re

KIND = "foreground sleep under a TERM/INT trap; use `sleep N & wait $!`"
# `trap - 1 2 15` resets a trap; `trap -- handler TERM` sets one.
_TRAP = re.compile(
    r"^\s*trap\s+(?:--\s+)?(?!-(?:\s|$))(?:'[^']*'|\"[^\"]*\"|\S+)\s+"
    r"(?:.*\s)?(?:(?:SIG)?(?:TERM|INT)|2|15)(?:\s|$)"
)
# `sleep` in command position, so `echo sleep 1` is not a sleep.
_SLEEP = re.compile(r"(?:^|[;&|({]|\b(?:then|do|else))\s*sleep\s+[^\s;&|]+(?![^\s;&|])")
_BACKGROUND = re.compile(r"\s*&(?!&)")
_WAIT = re.compile(r"\bwait\b")


def scan_source(source: str) -> list[int]:
    lines = [text.split("#", 1)[0] for text in source.splitlines()]
    if not any(_TRAP.search(text) for text in lines):
        return []
    hits = []
    for index, text in enumerate(lines):
        for match in _SLEEP.finditer(text):
            rest = text[match.end() :]
            following = [line for line in lines[index + 1 :] if line.strip()][:2]
            if _BACKGROUND.match(rest) and (
                _WAIT.search(rest) or any(_WAIT.search(line) for line in following)
            ):
                continue
            hits.append(index + 1)
            break
    return hits
