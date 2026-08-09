from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/agent-package-metadata"
SHA = "0123456789abcdef0123456789abcdef01234567"


def run_metadata(*arguments: str, root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, root / "scripts/agent-package-metadata", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def metadata_workspace(tmp_path: Path) -> Path:
    if not SCRIPT.is_file():
        pytest.skip("agent-package-metadata has not been implemented")
    workspace = tmp_path / "workspace"
    for relative in (
        "Cargo.toml",
        "pyproject.toml",
        "control/pyproject.toml",
        "agent/pyproject.toml",
        "scripts/agent-package-metadata",
    ):
        source = ROOT / relative
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return workspace


def test_development_metadata_emits_canonical_debian_outputs() -> None:
    result = run_metadata("development", "branch", "main", SHA, "1786300000")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=0.1.0~dev.1786300000+g0123456789ab",
        "next_version=0.1.0~dev.1786300001+g0123456789ab",
        "package=vonk-forge-agent_0.1.0~dev.1786300000+g0123456789ab_arm64.deb",
        f"artifact_name=vonk-agent-development-{SHA}",
        "channel=dev",
        "snapshot=dev-0.1.0~dev.1786300000+g0123456789ab",
    ]


def test_production_metadata_emits_canonical_stable_outputs() -> None:
    result = run_metadata("production", "tag", "v0.1.0", SHA, "1786300000")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=0.1.0",
        "next_version=0.1.1",
        "package=vonk-forge-agent_0.1.0_arm64.deb",
        f"artifact_name=vonk-agent-production-{SHA}",
        "channel=stable",
        "snapshot=stable-0.1.0",
    ]


@pytest.mark.parametrize(
    ("channel", "ref_type", "ref_name", "sha", "epoch"),
    (
        ("development", "tag", "main", SHA, "1786300000"),
        ("development", "branch", "release", SHA, "1786300000"),
        ("production", "branch", "v0.1.0", SHA, "1786300000"),
        ("production", "tag", "0.1.0", SHA, "1786300000"),
        ("production", "tag", "v0.1.1", SHA, "1786300000"),
        ("development", "branch", "main", SHA.upper(), "1786300000"),
        ("development", "branch", "main", SHA[:-1], "1786300000"),
        ("development", "branch", "main", SHA, "-1"),
        ("development", "branch", "main", SHA, "4102444800"),
        ("development", "branch", "main", SHA, "not-an-epoch"),
    ),
)
def test_metadata_rejects_noncanonical_release_inputs(
    channel: str, ref_type: str, ref_name: str, sha: str, epoch: str
) -> None:
    result = run_metadata(channel, ref_type, ref_name, sha, epoch)

    assert result.returncode == 64
    assert result.stdout == ""
    assert "agent package metadata is invalid" in result.stderr


def test_metadata_rejects_mismatched_workspace_versions(tmp_path: Path) -> None:
    workspace = metadata_workspace(tmp_path)
    agent_project = workspace / "agent/pyproject.toml"
    agent_project.write_text(agent_project.read_text().replace('version = "0.1.0"', 'version = "0.1.1"'))

    result = run_metadata("production", "tag", "v0.1.0", SHA, "1786300000", root=workspace)

    assert result.returncode == 64
    assert result.stdout == ""
    assert "agent package metadata is invalid" in result.stderr


def test_debian_ordering_promotes_development_to_final() -> None:
    current = "0.1.0~dev.1786300000+g0123456789ab"
    next_version = "0.1.0~dev.1786300001+g0123456789ab"

    lower = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", current, "lt", "0.1.0"],
        check=False,
        capture_output=True,
        text=True,
    )
    higher = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", next_version, "gt", current],
        check=False,
        capture_output=True,
        text=True,
    )

    assert lower.returncode == 0, lower.stderr
    assert higher.returncode == 0, higher.stderr
