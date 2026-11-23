from __future__ import annotations

import sys
from collections.abc import Sequence

from .check import check_source
from .patch import changed_lines
from .scans import MODES, ROOT


def main(*, modes: Sequence[str] = MODES) -> int:
    messages = []
    changes, removed = changed_lines(sys.stdin.read())
    for path, lines in changes.items():
        target = ROOT / path
        if target.is_file() and target.resolve().is_relative_to(ROOT):
            messages.extend(
                check_source(
                    path,
                    target.read_text(),
                    lines,
                    modes=modes,
                    removed=removed.get(path, ()),
                )
            )
    for message in messages:
        print(message, file=sys.stderr)
    return int(bool(messages))
