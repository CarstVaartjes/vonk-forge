"""Explicit run-local trust inputs used only by candidate acceptance harnesses."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mode() -> bool:
    return os.environ.get("VONK_ACCEPTANCE_TEST_MODE") == "1"


def release_public_key() -> Path:
    if test_mode():
        return Path(os.environ["VONK_ACCEPTANCE_RELEASE_PUBLIC_KEY"])
    return ROOT / "install/installer-release-public.pem"
