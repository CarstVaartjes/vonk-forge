"""Prebuilt Controller images for the image-content checks.

The ``built_image`` tests never build an image themselves. The "Controller
image build tests" CI job builds the Controller (``api``) and ``worker``
targets of ``control/Dockerfile`` once, with a build cache, and names them in
``VONK_TEST_CONTROLLER_IMAGE`` and ``VONK_TEST_WORKER_IMAGE``; each test is then
a quick check against that image. ``scripts/test-local --with-image-build``
does the same locally. Without the variable the test skips locally and fails
in CI, where only that job selects these tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _built_image(variable: str) -> str:
    image = os.environ.get(variable, "")
    reason = None
    if not image:
        reason = (
            f"{variable} names no prebuilt image; run scripts/test-local "
            "--with-image-build or build control/Dockerfile and set it"
        )
    elif shutil.which("docker") is None:
        reason = "Docker is unavailable"
    elif (
        subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, check=False
        ).returncode
        != 0
    ):
        reason = f"{variable}={image} is not a local Docker image"
    if reason is None:
        return image
    if os.getenv("CI", "").lower() == "true":
        pytest.fail(f"CI prerequisite missing: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def controller_image() -> str:
    return _built_image("VONK_TEST_CONTROLLER_IMAGE")


@pytest.fixture(scope="session")
def worker_image() -> str:
    return _built_image("VONK_TEST_WORKER_IMAGE")
