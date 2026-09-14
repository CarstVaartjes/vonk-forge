"""Acquire pinned image inputs separately from the container checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def control_image_build_args() -> list[str]:
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True, check=False
    ).returncode != 0:
        if os.getenv("CI"):
            pytest.fail("Docker is required for Controller image checks", pytrace=False)
        pytest.skip("Docker is unavailable")

    bases = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())["build_bases"]
    arguments = []
    for name in ("node", "python", "skopeo"):
        image = bases[name]
        # Local images are immutable inputs. Reuse them without contacting the
        # registry; only missing inputs enter the bounded acquisition retry.
        if subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, check=False
        ).returncode != 0:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/retry-dependency-fetch"), "docker", "pull", image],
                check=True,
            )
        arguments.extend(["--build-arg", f"{name.upper()}_IMAGE={image}"])
    return arguments
