from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/container-release-metadata"
SHA = "0123456789abcdef0123456789abcdef01234567"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_stable_tag_emits_exact_public_package_metadata() -> None:
    result = run("tag", "v0.1.0", SHA)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=0.1.0",
        "image_version_tag=v0.1.0",
        f"commit_tag=sha-{SHA}",
        f"dev_tag=dev-sha-{SHA}",
        "latest_alias=latest",
        "api_image=ghcr.io/carstvaartjes/vonk-forge-api",
        "worker_image=ghcr.io/carstvaartjes/vonk-forge-worker",
        (
            "api_dev_source=ghcr.io/carstvaartjes/vonk-forge-api:"
            f"dev-sha-{SHA}"
        ),
        (
            "worker_dev_source=ghcr.io/carstvaartjes/vonk-forge-worker:"
            f"dev-sha-{SHA}"
        ),
        "hermes_image=ghcr.io/carstvaartjes/vonk-forge-hermes",
        (
            "deployment_bundle_repository="
            "ghcr.io/carstvaartjes/vonk-forge-control-deployment"
        ),
        "platform_channel=stable",
    ]


@pytest.mark.parametrize(
    ("ref_type", "ref_name", "commit"),
    (
        ("branch", "v1.2.3", SHA),
        ("tag", "1.2.3", SHA),
        ("tag", "v01.2.3", SHA),
        ("tag", "v1.2", SHA),
        ("tag", "v1.2.3-rc.1", SHA),
        ("tag", "v1.2.3+build.1", SHA),
        ("tag", "v1.2.3", SHA),
        ("tag", "v1.2.3", SHA.upper()),
        ("tag", "v1.2.3", SHA[:-1]),
    ),
)
def test_non_release_input_fails_closed(
    ref_type: str, ref_name: str, commit: str
) -> None:
    result = run(ref_type, ref_name, commit)
    assert result.returncode == 64
    assert result.stdout == ""
    assert "release metadata is invalid" in result.stderr
