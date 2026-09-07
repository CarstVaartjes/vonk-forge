from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/agent-apt-metadata"


def run_metadata(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("channel", "version", "expected"),
    (
        (
            "dev",
            "0.1.0~dev.1786300000+g0123456789ab",
            [
                "repository=vonk-forge-dev",
                "distribution=dev",
                "public_prefix=dists/dev",
                "keyring=vonk-forge-dev-archive-keyring.gpg",
                "snapshot=dev-0.1.0~dev.1786300000+g0123456789ab",
                "state_prefix=arm64/versions/0.1.0~dev.1786300000+g0123456789ab",
            ],
        ),
        (
            "stable",
            "0.1.0",
            [
                "repository=vonk-forge",
                "distribution=stable",
                "public_prefix=dists/stable",
                "keyring=vonk-forge-archive-keyring.gpg",
                "snapshot=stable-0.1.0",
                "state_prefix=arm64/versions/0.1.0",
            ],
        ),
    ),
)
def test_metadata_emits_only_fixed_channel_authority(
    channel: str, version: str, expected: list[str]
) -> None:
    result = run_metadata(channel, version)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected


@pytest.mark.parametrize(
    "arguments",
    (
        (),
        ("dev",),
        ("testing", "0.1.0"),
        ("dev", "0.1.0"),
        ("stable", "0.1.0~dev.1786300000+g0123456789ab"),
        ("stable", "01.1.0"),
        ("dev", "0.1.0~dev.0+g0123456789AB"),
        ("dev", "0.1.0~dev.1+g0123456789ab", "vonk-forge"),
    ),
)
def test_metadata_rejects_noncanonical_or_caller_selected_authority(
    arguments: tuple[str, ...],
) -> None:
    result = run_metadata(*arguments)

    assert result.returncode == 64
    assert result.stdout == ""
    assert result.stderr == "agent apt metadata is invalid\n"
