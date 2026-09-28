"""Functional package and CLI boundaries for the greenfield installer."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from cluster_profiles.cli import main

ROOT = Path(__file__).resolve().parents[1]


class _UnexpectedClient:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"removed update command reached client method {name}")


def test_operator_cli_rejects_removed_platform_update_commands() -> None:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(
            ("admin", "updates", "skew", "--json"),
            control_client=_UnexpectedClient(),
        )
    assert result == 2
    assert json.loads(stdout.getvalue())["error_type"] == "arguments"
    assert stderr.getvalue() == ""


HATCHLING_WHEEL_BUILD = (
    sys.executable,
    "-m",
    "hatchling",
    "build",
    "--target",
    "wheel",
    "--directory",
)


def _build_wheel(tmp_path: Path, project: Path | None = None) -> set[str]:
    output = tmp_path / ("control-dist" if project else "operator-dist")
    # The build backend is locked in the test environment; building with it
    # directly needs no isolated build environment and no network.
    subprocess.run(
        [*HATCHLING_WHEEL_BUILD, str(output)],
        cwd=project or ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    with zipfile.ZipFile(next(output.glob("*.whl"))) as archive:
        return set(archive.namelist())


def test_control_wheel_exposes_no_host_updater_entrypoint(tmp_path: Path) -> None:
    names = _build_wheel(tmp_path, ROOT / "control")
    assert not any(name.endswith(".dist-info/entry_points.txt") for name in names)


def test_operator_wheel_exposes_no_local_ssh_fleet_runtime(tmp_path: Path) -> None:
    names = _build_wheel(tmp_path)
    assert "cluster_profiles/placement.py" not in names
    assert not any(name.startswith("cluster_profiles/fleet/") for name in names)
