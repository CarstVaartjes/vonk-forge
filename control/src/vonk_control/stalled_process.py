"""Run a process until it finishes or its own progress stops: a stall, not a total.

A long copy (a multi-gigabyte image) is healthy however long it takes while it
keeps writing bytes. Only a whole window in which the measured progress does
not change ends it, and the caller reports that stall with its evidence.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stall_window import Verdict, progress_verdict


def tree_bytes(*roots: Path) -> int:
    """Bytes stored under ``roots``; a vanished file or directory counts as none."""

    total = 0
    pending = [root for root in roots]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def run_until_stalled(
    command: Sequence[str],
    *,
    progress: Callable[[], int],
    window: timedelta,
    env: dict[str, str] | None = None,
    poll_seconds: float = 10.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` whose bound is a stall window over ``progress()``.

    Raises ``subprocess.TimeoutExpired`` (after killing the process) only when
    ``progress()`` did not change for a whole ``window``.
    """

    started = clock()
    progressed: datetime | None = None
    last = progress()
    with subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    ) as process:
        while (
            progress_verdict(
                started_at=started, progressed_at=progressed, window=window, now=clock()
            )
            is not Verdict.STALLED
        ):
            try:
                stdout, stderr = process.communicate(timeout=poll_seconds)
            except subprocess.TimeoutExpired:
                current = progress()
                if current != last:
                    last, progressed = current, clock()
                continue
            return subprocess.CompletedProcess(
                list(command), process.returncode, stdout, stderr
            )
        process.kill()
        process.communicate(timeout=poll_seconds)
    raise subprocess.TimeoutExpired(
        list(command),
        window.total_seconds(),
        output=f"no progress for {int(window.total_seconds())}s",
    )
