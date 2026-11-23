"""The live follow view: a redrawn bar block on a terminal, spaced lines otherwise."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Mapping
from enum import Enum
from typing import TextIO

from .progress import live_lines, phase_key, progress_line

#: A pipe or log gets a line when the phase changes and at most this often.
PLAIN_INTERVAL_SECONDS = 30.0


class _Screen(Enum):
    """Whether a redrawable block is the last thing on a terminal."""

    CLEAN = "clean"
    DRAWN = "drawn"


class FollowFeed:
    """Show each observation of a followed operation to a person.

    A terminal redraws one block per phase in place (overall bar, then a bar per
    Spark) and starts a new block when the phase changes.  Anything else gets a
    plain line on each phase change and at most every ``PLAIN_INTERVAL_SECONDS``.
    """

    def __init__(
        self,
        out: TextIO,
        *,
        tty: bool,
        clean: Callable[[str], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._out = out
        self._tty = tty
        self._clean = clean
        self._clock = clock
        self._screen = _Screen.CLEAN
        self._key: str | None = None
        self._drawn = 0
        self._last_line_at = 0.0

    def show(self, observed: object, note: str = "") -> None:
        if not isinstance(observed, Mapping):
            return
        key = phase_key(observed)
        if self._tty:
            self._redraw(key, live_lines(observed), note)
            return
        now = self._clock()
        if key == self._key and now - self._last_line_at < PLAIN_INTERVAL_SECONDS:
            return
        self._key, self._last_line_at = key, now
        print(self._clean(f"{note}{progress_line(observed)}"), file=self._out)

    def close(self) -> None:
        """Nothing is left half-drawn: the block is already newline-terminated."""
        self._screen = _Screen.CLEAN
        self._out.flush()

    def _redraw(self, key: str, lines: list[str], note: str) -> None:
        width = max(20, shutil.get_terminal_size().columns - 1)
        lines = [self._clean(f"{note}{lines[0]}"), *map(self._clean, lines[1:])]
        if self._screen is _Screen.DRAWN and key == self._key:
            self._out.write(f"\x1b[{self._drawn}F")
        self._out.write(
            "".join(f"\x1b[2K{line[:width]}\n" for line in lines) + "\x1b[J"
        )
        self._out.flush()
        self._screen, self._key, self._drawn = _Screen.DRAWN, key, len(lines)
