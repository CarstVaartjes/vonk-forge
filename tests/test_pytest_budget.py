"""Over-budget tests fail locally and only warn in CI up to twice the budget."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, ci: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_sleep.py").write_text(
        "import time\n\ndef test_sleeps():\n    time.sleep(0.15)\n", encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.pytest_budget",
            # A 0.1 s budget: the 0.15 s test is over it but under twice it.
            "--test-budget-scale=0.01",
            "--rootdir",
            str(tmp_path),
            "-c",
            os.devnull,
            str(tmp_path / "test_sleep.py"),
        ],
        cwd=ROOT,
        env={**os.environ, "CI": ci, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_over_budget_fails_locally_and_warns_in_ci(tmp_path: Path) -> None:
    local = _run(tmp_path, "")
    assert local.returncode == 1
    assert "over its 0.1s budget" in local.stdout

    ci = _run(tmp_path, "true")
    assert ci.returncode == 0, ci.stdout
    assert "::warning::" in ci.stdout and "test_sleeps" in ci.stdout
