"""A process runs as long as its output grows; only a silent window ends it."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from vonk_control.stalled_process import run_until_stalled, tree_bytes

_WRITER = """
import sys, time
with open(sys.argv[1], "ab", buffering=0) as out:
    for _ in range(int(sys.argv[2])):
        out.write(b"x")
        time.sleep(float(sys.argv[3]))
"""


def test_a_copy_that_keeps_writing_outlives_several_windows(tmp_path: Path) -> None:
    """Catches a total timeout cutting a long copy that is still writing."""

    target = tmp_path / "copy"
    window = timedelta(seconds=0.4)
    # Writes for about 1.5 s, more than three windows, never silent for one.
    result = run_until_stalled(
        [sys.executable, "-c", _WRITER, str(target), "15", "0.1"],
        progress=lambda: tree_bytes(tmp_path),
        window=window,
        poll_seconds=0.05,
    )
    assert result.returncode == 0
    assert target.stat().st_size == 15


def test_a_copy_that_stops_writing_ends_after_one_silent_window(
    tmp_path: Path,
) -> None:
    """A copy whose bytes stop is killed one window later, with that evidence."""

    with pytest.raises(subprocess.TimeoutExpired) as stalled:
        run_until_stalled(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            progress=lambda: tree_bytes(tmp_path),
            window=timedelta(seconds=0.3),
            poll_seconds=0.05,
        )
    assert "no progress" in str(stalled.value.output)
