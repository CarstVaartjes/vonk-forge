from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
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
    workspace = tmp_path / "workspace"
    for relative in (
        "Cargo.toml",
        "pyproject.toml",
        "control/pyproject.toml",
        "scripts/agent-package-metadata",
    ):
        source = ROOT / relative
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return workspace


def test_development_metadata_emits_canonical_debian_outputs() -> None:
    result = run_metadata("development", "branch", "main", SHA, "417")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=0.1.0~dev.417+g0123456789ab",
        "next_version=0.1.0~dev.418+g0123456789ab",
        "baseline_version=0.0.0~acceptance.1+g0123456789ab",
        "arm64_package=vonk-forge-agent_0.1.0~dev.417+g0123456789ab_arm64.deb",
        "amd64_package=vonk-forge-agent_0.1.0~dev.417+g0123456789ab_amd64.deb",
        "arm64_baseline_package=vonk-forge-agent_0.0.0~acceptance.1+g0123456789ab_arm64.deb",
        "amd64_baseline_package=vonk-forge-agent_0.0.0~acceptance.1+g0123456789ab_amd64.deb",
        f"artifact_name=vonk-agent-development-{SHA}",
        f"baseline_artifact_name=vonk-agent-development-{SHA}-acceptance-baseline",
        "channel=dev",
        "snapshot=dev-0.1.0~dev.417+g0123456789ab",
    ]


def test_production_metadata_emits_canonical_stable_outputs() -> None:
    result = run_metadata("production", "tag", "v0.1.0", SHA, "0")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=0.1.0",
        "next_version=0.1.0+lifecycle.1",
        "baseline_version=0.0.0~acceptance.1+g0123456789ab",
        "arm64_package=vonk-forge-agent_0.1.0_arm64.deb",
        "amd64_package=vonk-forge-agent_0.1.0_amd64.deb",
        "arm64_baseline_package=vonk-forge-agent_0.0.0~acceptance.1+g0123456789ab_arm64.deb",
        "amd64_baseline_package=vonk-forge-agent_0.0.0~acceptance.1+g0123456789ab_amd64.deb",
        f"artifact_name=vonk-agent-production-{SHA}",
        f"baseline_artifact_name=vonk-agent-production-{SHA}-acceptance-baseline",
        "channel=stable",
        "snapshot=stable-0.1.0",
    ]


@pytest.mark.parametrize(
    ("channel", "ref_type", "ref_name", "sha", "sequence"),
    (
        ("development", "tag", "main", SHA, "417"),
        ("development", "branch", "release", SHA, "417"),
        ("production", "branch", "v0.1.0", SHA, "0"),
        ("production", "tag", "0.1.0", SHA, "0"),
        ("production", "tag", "v0.1.1", SHA, "0"),
        ("development", "branch", "main", SHA.upper(), "417"),
        ("development", "branch", "main", SHA[:-1], "417"),
        ("development", "branch", "main", SHA, "0"),
        ("development", "branch", "main", SHA, "-1"),
        ("development", "branch", "main", SHA, "0417"),
        ("development", "branch", "main", SHA, "not-a-sequence"),
        ("development", "branch", "main", SHA, "9999999999999999999"),
        ("development", "branch", "main", SHA, "10000000000000000000"),
        ("production", "tag", "v0.1.0", SHA, "417"),
    ),
)
def test_metadata_rejects_noncanonical_release_inputs(
    channel: str, ref_type: str, ref_name: str, sha: str, sequence: str
) -> None:
    result = run_metadata(channel, ref_type, ref_name, sha, sequence)

    assert result.returncode == 64
    assert result.stdout == ""
    assert "agent package metadata is invalid" in result.stderr


def test_metadata_rejects_mismatched_workspace_versions(tmp_path: Path) -> None:
    workspace = metadata_workspace(tmp_path)
    control_project = workspace / "control/pyproject.toml"
    control_project.write_text(
        control_project.read_text().replace('version = "0.1.0"', 'version = "0.1.1"')
    )

    result = run_metadata(
        "production", "tag", "v0.1.0", SHA, "0", root=workspace
    )

    assert result.returncode == 64
    assert result.stdout == ""
    assert "agent package metadata is invalid" in result.stderr


def test_debian_ordering_promotes_development_to_final() -> None:
    current = "0.1.0~dev.417+g0123456789ab"

    lower = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", current, "lt", "0.1.0"],
        check=False,
        capture_output=True,
        text=True,
    )
    higher = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", "0.1.0", "gt", current],
        check=False,
        capture_output=True,
        text=True,
    )

    assert lower.returncode == 0, lower.stderr
    assert higher.returncode == 0, higher.stderr


def test_debian_ordering_uses_publication_sequence_before_sha() -> None:
    earlier = run_metadata("development", "branch", "main", "f" * 40, "417")
    later = run_metadata("development", "branch", "main", "0" * 40, "418")

    assert earlier.returncode == 0, earlier.stderr
    assert later.returncode == 0, later.stderr
    earlier_version = earlier.stdout.splitlines()[0].removeprefix("version=")
    later_version = later.stdout.splitlines()[0].removeprefix("version=")
    ordered = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", later_version, "gt", earlier_version],
        check=False,
        capture_output=True,
        text=True,
    )

    assert ordered.returncode == 0, ordered.stderr
