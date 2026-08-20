from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cluster_profiles.platform_release import PlatformRelease
from tests.scripts.test_publish_control_deployment_bundle import (
    _bundle_descriptor,
    _release,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-platform-manifest"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    descriptor = _bundle_descriptor(b"bundle")
    release = _release(descriptor)
    del release["deployment_bundle"]
    source = tmp_path / "platform-input.json"
    descriptor_path = tmp_path / "bundle-descriptor.json"
    source.write_bytes(_canonical(release))
    descriptor_path.write_bytes(_canonical(descriptor))
    for package in release["agent_packages"]:
        architecture = package["architecture"]
        evidence = {
            "package": package,
            "locator": f"agent_packages.{architecture}",
            "schema_version": 1,
        }
        (tmp_path / f"{architecture}-package-evidence.json").write_bytes(
            _canonical(evidence)
        )
    return source, descriptor_path, descriptor


def _default_agent_evidence(tmp_path: Path) -> tuple[str, ...]:
    return (
        "--agent-package-evidence",
        str(tmp_path / "linux-arm64-package-evidence.json"),
        "--agent-package-evidence",
        str(tmp_path / "linux-amd64-package-evidence.json"),
    )


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_builder_assembles_and_writes_canonical_v2_manifest(tmp_path: Path) -> None:
    source, descriptor_path, descriptor = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        *_default_agent_evidence(tmp_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(document)
    assert document["deployment_bundle"] == descriptor
    assert PlatformRelease.from_bytes(output.read_bytes()).platform_version == "1.2.0"
    receipt = json.loads(result.stdout)
    assert receipt["platform_version"] == "1.2.0"
    assert receipt["target_name"].startswith("platform/releases/1.2.0/")
    assert receipt["target_sha256"] in receipt["target_name"]


def test_builder_rejects_noncanonical_input_and_version_disagreement(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    source.write_text(json.dumps(json.loads(source.read_bytes()), indent=2))

    noncanonical = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.2.0",
        "--output",
        str(tmp_path / "first.json"),
    )

    assert noncanonical.returncode == 2
    assert "canonical" in noncanonical.stderr
    source.write_bytes(_canonical(json.loads(source.read_bytes())))
    mismatch = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.3.0",
        "--output",
        str(tmp_path / "second.json"),
    )
    assert mismatch.returncode == 2
    assert "version" in mismatch.stderr


def test_builder_refuses_to_overwrite_output(tmp_path: Path) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"
    output.write_text("existing\n")

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert output.read_text() == "existing\n"


def test_builder_replaces_review_input_artifact_with_ci_evidence(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"
    evidence_path = tmp_path / "api-evidence.json"
    evidence = {
        "artifact": {
            "name": "api",
            "provenance_sha256": "d" * 64,
            "reference": f"ghcr.io/example/api@sha256:{'e' * 64}",
            "sbom_sha256": "c" * 64,
            "sha256": "e" * 64,
            "size": 2048,
        },
        "locator": "control.images.api",
        "schema_version": 1,
    }
    evidence_path.write_bytes(_canonical(evidence))

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--artifact-evidence",
        str(evidence_path),
        *_default_agent_evidence(tmp_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert (
        json.loads(output.read_bytes())["control"]["images"]["api"]
        == evidence["artifact"]
    )


def test_builder_rejects_single_architecture_agent_package_release_input(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    source_document = json.loads(source.read_bytes())
    source_document["agent_packages"] = [
        {
            "architecture": "linux-arm64",
            "name": "vonk-forge-agent",
            "version": "1.2.0",
            "filename": "vonk-forge-agent_1.2.0_arm64.deb",
            "sha256": "0" * 64,
            "size": 4096,
            "sbom_sha256": "1" * 64,
            "provenance_sha256": "2" * 64,
            "sigstore_bundle_sha256": "3" * 64,
        }
    ]
    source.write_bytes(_canonical(source_document))
    output = tmp_path / "platform-release.json"
    evidence_path = tmp_path / "agent-package-evidence.json"
    evidence = {
        "package": {
            "architecture": "linux-arm64",
            "name": "vonk-forge-agent",
            "version": "1.2.0",
            "filename": "vonk-forge-agent_1.2.0_arm64.deb",
            "sha256": "a" * 64,
            "size": 8192,
            "sbom_sha256": "b" * 64,
            "provenance_sha256": "c" * 64,
            "sigstore_bundle_sha256": "d" * 64,
        },
        "locator": "agent_packages.linux-arm64",
        "schema_version": 1,
    }
    evidence_path.write_bytes(_canonical(evidence))

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--agent-package-evidence",
        str(evidence_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert "both agent package architectures" in result.stderr
    assert not output.exists()


def test_builder_requires_ci_evidence_for_both_agent_package_architectures(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    source_document = json.loads(source.read_bytes())
    packages = []
    evidence_paths = []
    for architecture, deb_architecture, digest in (
        ("linux-arm64", "arm64", "a"),
        ("linux-amd64", "amd64", "b"),
    ):
        package = {
            "architecture": architecture,
            "name": "vonk-forge-agent",
            "version": "1.2.0",
            "filename": f"vonk-forge-agent_1.2.0_{deb_architecture}.deb",
            "sha256": "0" * 64,
            "size": 4096,
            "sbom_sha256": "1" * 64,
            "provenance_sha256": "2" * 64,
            "sigstore_bundle_sha256": "3" * 64,
        }
        packages.append(package)
        evidence = {
            "package": package | {"sha256": digest * 64},
            "locator": f"agent_packages.{architecture}",
            "schema_version": 1,
        }
        evidence_path = tmp_path / f"{deb_architecture}-evidence.json"
        evidence_path.write_bytes(_canonical(evidence))
        evidence_paths.append(evidence_path)
    source_document["agent_packages"] = packages
    source.write_bytes(_canonical(source_document))

    missing = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--agent-package-evidence",
        str(evidence_paths[0]),
        "--version",
        "1.2.0",
        "--output",
        str(tmp_path / "missing.json"),
    )
    assert missing.returncode == 2
    assert "both agent package architectures" in missing.stderr

    complete = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--agent-package-evidence",
        str(evidence_paths[0]),
        "--agent-package-evidence",
        str(evidence_paths[1]),
        "--version",
        "1.2.0",
        "--output",
        str(tmp_path / "complete.json"),
    )
    assert complete.returncode == 0, complete.stderr
