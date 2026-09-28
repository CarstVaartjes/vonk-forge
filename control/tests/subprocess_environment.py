"""Explicit environments for subprocesses launched by controller tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn


def isolated_environment(
    home: Path, *, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Provide stable tool lookup and private user config without host settings."""

    home.mkdir(parents=True, exist_ok=True)
    tool_paths = [os.defpath]
    for tool in ("cargo", "git", "uv"):
        executable = shutil.which(tool)
        if executable:
            tool_paths.append(str(Path(executable).parent))
    environment = {
        "PATH": os.pathsep.join(dict.fromkeys(tool_paths)),
        "HOME": str(home),
        "TMPDIR": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_PYTHON": sys.executable,
    }
    # Offline uv builds use this explicit test-runner input. No other host
    # environment variables cross the subprocess boundary.
    if cache_dir := os.environ.get("UV_CACHE_DIR"):
        environment["UV_CACHE_DIR"] = cache_dir
    if extra:
        environment.update(extra)
    return environment


def prerequisite_unavailable(message: str) -> NoReturn:
    """Skip a local test whose required external fixture is unavailable."""

    import pytest

    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(f"CI prerequisite missing: {message}", pytrace=False)
    pytest.skip(message)


def cli_dependency_site_packages() -> Path:
    """site-packages of the CLI's locked runtime dependencies.

    scripts/sync-cli-dependencies prepares it (CI does so before the suites).
    A scratch venv links it instead of resolving or downloading anything.
    """

    root = Path(__file__).resolve().parents[2]
    environment = Path(
        os.environ.get("VONK_CLI_DEPENDENCY_ENV", root / ".cli-dependencies")
    )
    found = sorted(environment.glob("lib/python3.*/site-packages"))
    if not found:
        prerequisite_unavailable(
            "the CLI dependency environment is missing; run scripts/sync-cli-dependencies"
        )
    return found[0]


def install_cli_wheel(uv: str, venv: Path, wheel: Path, env: dict[str, str]) -> Path:
    """Install a built vonkctl wheel into a new venv without any resolution.

    The wheel goes in with --no-deps; its locked dependencies come from the
    prepared CLI dependency environment through a .pth file, so the venv sees
    the CLI and its dependencies only (no Controller, no pydantic).
    """

    dependencies = cli_dependency_site_packages()
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    python = venv / "bin" / "python"
    installed = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--compile-bytecode",
            "--python",
            str(python),
            str(wheel),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    [site_packages] = venv.glob("lib/python3.*/site-packages")
    (site_packages / "vonk-cli-dependencies.pth").write_text(
        f"{dependencies}\n", encoding="utf-8"
    )
    return python
