"""A process runs as long as its output grows; only a silent window ends it."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_control.stalled_process import run_until_stalled, tree_bytes

_WRITER = """
import pathlib, sys, time
deadline = time.monotonic() + 30
with open(sys.argv[1], "ab", buffering=0) as out:
    for index in range(1, int(sys.argv[3]) + 1):
        out.write(b"x")
        # Every write is observed, regardless of how the two processes are
        # scheduled. This is test synchronization, not the production clock.
        while not (pathlib.Path(sys.argv[2]) / str(index)).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not acknowledge the write")
            time.sleep(0.001)
"""


def test_a_copy_that_keeps_writing_outlives_several_windows(tmp_path: Path) -> None:
    """Catches a total timeout cutting a long copy that is still writing."""

    data = tmp_path / "data"
    data.mkdir()
    acknowledgements = tmp_path / "observed"
    acknowledgements.mkdir()
    target = data / "copy"
    window = timedelta(seconds=0.4)
    started = now = datetime.now(UTC)

    def progress() -> int:
        nonlocal now
        size = tree_bytes(data)
        # Charge half a window per observed byte. Host scheduling consumes no
        # stall budget, and even a fast child cannot skip the observed windows.
        now = started + size * window / 2
        (acknowledgements / str(size)).touch()
        return size

    result = run_until_stalled(
        [sys.executable, "-c", _WRITER, str(target), str(acknowledgements), "15"],
        progress=progress,
        window=window,
        poll_seconds=0.05,
        clock=lambda: now,
    )
    assert result.returncode == 0
    assert target.stat().st_size == 15
    assert now - started > 3 * window


def test_a_copy_that_stops_writing_ends_after_one_silent_window(
    tmp_path: Path,
) -> None:
    """A copy whose bytes stop is killed one window later, with that evidence."""

    now = datetime.now(UTC)
    window = timedelta(seconds=0.3)
    observations = 0

    def progress() -> int:
        nonlocal now, observations
        if observations:
            now += window
        observations += 1
        return tree_bytes(tmp_path)

    with pytest.raises(subprocess.TimeoutExpired) as stalled:
        run_until_stalled(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            progress=progress,
            window=window,
            poll_seconds=0.05,
            clock=lambda: now,
        )
    assert observations == 2
    assert "no progress" in str(stalled.value.output)
