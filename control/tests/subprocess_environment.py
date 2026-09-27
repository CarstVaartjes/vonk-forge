"""Explicit environments for subprocesses launched by controller tests."""

from __future__ import annotations

import os
import shutil
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
