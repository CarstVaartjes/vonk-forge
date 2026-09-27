"""Expose the hardware-independent Bash regression suites to pytest."""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.linux_only

ROOT = Path(__file__).resolve().parents[1]
SHELL_SUITES = tuple(sorted(ROOT.glob("tests/**/test_*.sh")))


def _suite_id(suite: Path) -> str:
    return suite.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("suite", SHELL_SUITES, ids=_suite_id)
def test_shell_suite(
    suite: Path, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if "systemd" in suite.name:
        request.node.add_marker(pytest.mark.needs_systemd)
    temporary_home = tmp_path / "home"
    temporary_home.mkdir()
    environment = {
        "HOME": str(temporary_home),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(temporary_home),
        "XDG_CONFIG_HOME": str(temporary_home / ".config"),
        "XDG_CACHE_HOME": str(temporary_home / ".cache"),
    }
    completed = subprocess.run(
        ["bash", str(suite)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if completed.returncode == 77:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(
                f"{_suite_id(suite)} has missing prerequisites in CI: "
                f"{completed.stderr.strip()}"
            )
        pytest.skip(completed.stderr.strip() or "suite prerequisites are unavailable")

    assert completed.returncode == 0, (
        f"{_suite_id(suite)} exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
