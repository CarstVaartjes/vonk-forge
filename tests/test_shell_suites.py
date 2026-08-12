"""Expose the hardware-independent Bash regression suites to pytest."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL_SUITES = tuple(sorted(ROOT.glob("tests/**/test_*.sh")))


def _suite_id(suite: Path) -> str:
    return suite.relative_to(ROOT).as_posix()


def test_shell_suites_are_present() -> None:
    assert SHELL_SUITES, "no Bash regression suites were discovered"


@pytest.mark.parametrize("suite", SHELL_SUITES, ids=_suite_id)
def test_shell_suite(suite: Path) -> None:
    completed = subprocess.run(
        ["bash", str(suite)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if completed.returncode == 77:
        pytest.skip(completed.stderr.strip() or "suite prerequisites are unavailable")

    assert completed.returncode == 0, (
        f"{_suite_id(suite)} exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
