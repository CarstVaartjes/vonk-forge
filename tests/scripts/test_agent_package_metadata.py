from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
_MAJOR, _MINOR, _PATCH = (int(part) for part in VERSION.split("."))
NEXT_PATCH = f"{_MAJOR}.{_MINOR}.{_PATCH + 1}"
# The acceptance baseline is the next lower release (no lower one is a script error).
_LOWER = (
    f"{_MAJOR}.{_MINOR}.{_PATCH - 1}"
    if _PATCH
    else f"{_MAJOR}.{_MINOR - 1}.0"
    if _MINOR
    else f"{_MAJOR - 1}.0.0"
)
BASELINE = f"{_LOWER}~acceptance.1+g{SHA[:12]}"


def run_metadata(
    *arguments: str, root: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, root / "scripts/agent/agent-package-metadata", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(root), "PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )


def metadata_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for relative in (
        "Cargo.toml",
        "pyproject.toml",
        "control/pyproject.toml",
        "nas_setup/pyproject.toml",
        "scripts/agent/agent-package-metadata",
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
        f"version={VERSION}~dev.417+g0123456789ab",
        f"next_version={VERSION}~dev.418+g0123456789ab",
        f"baseline_version={BASELINE}",
        f"arm64_package=vonk-forge-agent_{VERSION}~dev.417+g0123456789ab_arm64.deb",
        f"arm64_baseline_package=vonk-forge-agent_{BASELINE}_arm64.deb",
        f"artifact_name=vonk-agent-development-{SHA}",
        f"baseline_artifact_name=vonk-agent-development-{SHA}-acceptance-baseline",
        "channel=dev",
        f"snapshot=dev-{VERSION}~dev.417+g0123456789ab",
    ]


def test_production_metadata_emits_canonical_stable_outputs() -> None:
    result = run_metadata("production", "tag", f"v{VERSION}", SHA, "0")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"version={VERSION}",
        f"next_version={VERSION}+lifecycle.1",
        f"baseline_version={BASELINE}",
        f"arm64_package=vonk-forge-agent_{VERSION}_arm64.deb",
        f"arm64_baseline_package=vonk-forge-agent_{BASELINE}_arm64.deb",
        f"artifact_name=vonk-agent-production-{SHA}",
        f"baseline_artifact_name=vonk-agent-production-{SHA}-acceptance-baseline",
        "channel=stable",
        f"snapshot=stable-{VERSION}",
    ]


@pytest.mark.parametrize(
    ("channel", "ref_type", "ref_name", "sha", "sequence"),
    [
        ("development", "tag", "main", SHA, "417"),
        ("development", "branch", "release", SHA, "417"),
        ("production", "branch", "v0.1.0", SHA, "0"),
        ("production", "tag", "0.1.0", SHA, "0"),
        ("production", "tag", f"v{NEXT_PATCH}", SHA, "0"),
        ("development", "branch", "main", SHA.upper(), "417"),
        ("development", "branch", "main", SHA[:-1], "417"),
        ("development", "branch", "main", SHA, "0"),
        ("development", "branch", "main", SHA, "-1"),
        ("development", "branch", "main", SHA, "0417"),
        ("development", "branch", "main", SHA, "not-a-sequence"),
        ("development", "branch", "main", SHA, "9999999999999999999"),
        ("development", "branch", "main", SHA, "10000000000000000000"),
        ("production", "tag", f"v{VERSION}", SHA, "417"),
    ],
)
def test_metadata_rejects_noncanonical_release_inputs(
    channel: str, ref_type: str, ref_name: str, sha: str, sequence: str
) -> None:
    result = run_metadata(channel, ref_type, ref_name, sha, sequence)

    assert result.returncode == 64
    assert result.stdout == ""
    repaired = run_metadata("production", "tag", f"v{VERSION}", SHA, "0")
    assert repaired.returncode == 0, repaired.stderr
    assert repaired.stdout


def test_metadata_rejects_mismatched_workspace_versions(tmp_path: Path) -> None:
    workspace = metadata_workspace(tmp_path)
    control_project = workspace / "control/pyproject.toml"
    control_project.write_text(
        control_project.read_text().replace(
            f'version = "{VERSION}"', f'version = "{NEXT_PATCH}"'
        )
    )

    result = run_metadata("production", "tag", f"v{VERSION}", SHA, "0", root=workspace)

    assert result.returncode == 64
    assert result.stdout == ""
    control_project.write_text(
        control_project.read_text().replace(
            f'version = "{NEXT_PATCH}"', f'version = "{VERSION}"'
        )
    )
    repaired = run_metadata(
        "production", "tag", f"v{VERSION}", SHA, "0", root=workspace
    )
    assert repaired.returncode == 0, repaired.stderr
    assert repaired.stdout


@pytest.mark.needs_dpkg
def test_debian_ordering_promotes_development_to_final() -> None:
    current = "0.1.1~dev.417+g0123456789ab"

    lower = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", current, "lt", "0.1.1"],
        check=False,
        capture_output=True,
        text=True,
    )
    higher = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", "0.1.1", "gt", current],
        check=False,
        capture_output=True,
        text=True,
    )

    assert lower.returncode == 0, lower.stderr
    assert higher.returncode == 0, higher.stderr


@pytest.mark.needs_dpkg
def test_current_development_line_is_newer_than_last_stable() -> None:
    result = run_metadata("development", "branch", "main", SHA, "417")

    assert result.returncode == 0, result.stderr
    current = result.stdout.splitlines()[0].removeprefix("version=")
    ordered = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", current, "gt", "0.1.0"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert ordered.returncode == 0, ordered.stderr


@pytest.mark.needs_dpkg
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
