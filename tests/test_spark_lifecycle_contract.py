from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = ROOT / "tests/acceptance/test_spark_lifecycle.py"
GENERATION = "a" * 64
SOURCE_SHA = "b" * 40
ARM64_PHASES = [
    "publication-graph-verified",
    "controller-ready",
    "baseline-installed",
    "paired",
    "synthetic-device-ready",
    "canary-completed",
    "identity-renewed",
    "candidate-upgraded",
    "direct-rust-agent-healthy",
]


def _publication_graph() -> dict[str, object]:
    return {
        "baseline_package_sha256": "3" * 64,
        "baseline_version": "1.2.2~acceptance.1+gbbbbbbbbbbbb",
        "candidate_package_sha256": "4" * 64,
        "candidate_version": "1.2.3",
        "channel": "dev",
        "generation": GENERATION,
        "images_sha256": "c" * 64,
        "packages": {
            "linux-amd64": {
                "baseline_sha256": "1" * 64,
                "candidate_sha256": "2" * 64,
            },
            "linux-arm64": {
                "baseline_sha256": "3" * 64,
                "candidate_sha256": "4" * 64,
            },
        },
        "platform": "linux-arm64",
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "verified_platforms": ["linux-amd64", "linux-arm64"],
    }


def _arm64_proof() -> dict[str, object]:
    node_id = "spk_0123456789abcdef0123456789abcdef"
    serial = "0123456789abcdef"
    return {
        "canary": {
            "completed_states": [
                "inventory-ready",
                "recipe-resolved",
                "source-verified",
                "image-built",
                "image-distributed",
                "installed",
                "running",
                "route-published",
                "inference-ok",
                "stopped",
                "route-withdrawn",
                "uninstalled",
            ],
            "deterministic_response_sha256": "5" * 64,
        },
        "config_sha256_after_upgrade": "6" * 64,
        "config_sha256_before_upgrade": "6" * 64,
        "controller_generation": GENERATION,
        "direct_agent_health": {
            "healthy": True,
            "implementation": "rust",
            "transport": "direct",
        },
        "installation": {
            "architecture": "arm64",
            "baseline": {
                "binary_sha256": "7" * 64,
                "build_sha256": "8" * 64,
                "package_sha256": "3" * 64,
                "version": "1.2.2~acceptance.1+gbbbbbbbbbbbb",
            },
            "candidate": {
                "binary_sha256": "9" * 64,
                "build_sha256": "a" * 64,
                "package_sha256": "4" * 64,
                "version": "1.2.3",
            },
        },
        "node_id_after_renewal": node_id,
        "node_id_after_upgrade": node_id,
        "node_id_before_renewal": node_id,
        "pairing_grant_use_count": 1,
        "private_identity_sha256_after_upgrade": "d" * 64,
        "private_identity_sha256_before_upgrade": "d" * 64,
        "publication_graph": _publication_graph(),
        "renewal": {
            "certificate_serial_after": "fedcba9876543210",
            "certificate_serial_before": serial,
            "old_certificate_rejection": {
                "durably_recorded": True,
                "rejected": True,
                "serial": serial,
            },
        },
        "synthetic_device": {
            "architecture": "linux-arm64",
            "cdi_name": "nvidia.com/gpu=all",
            "fixture_sha256": "e" * 64,
            "physical_gpu": False,
            "provenance": "ci-only-synthetic-cdi",
            "synthetic": True,
        },
    }


def _record(root: Path, relative: str, content: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": relative,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _graph_inputs(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    objects = tmp_path / "objects"
    candidate_artifacts = {
        f"agent-package-{platform}": _record(
            objects,
            f"artifacts/dev/releases/{GENERATION}/spark/current/{platform}/vonk-forge-agent.deb",
            f"candidate-{platform}".encode(),
        )
        for platform in ("linux-amd64", "linux-arm64")
    }
    baseline_artifacts = {
        f"agent-package-{platform}": _record(
            objects,
            f"artifacts/dev/releases/{GENERATION}/acceptance-baseline/spark/current/{platform}/vonk-forge-agent.deb",
            f"baseline-{platform}".encode(),
        )
        for platform in ("linux-amd64", "linux-arm64")
    }
    common = {
        "channel": "dev",
        "generation": GENERATION,
        "images": {
            "api": "ghcr.io/example/api:1.2.3@sha256:" + "c" * 64,
            "worker": "ghcr.io/example/worker:1.2.3@sha256:" + "d" * 64,
        },
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
    }
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(common | {"artifacts": candidate_artifacts, "version": "1.2.3"})
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            common
            | {
                "acceptance_only": True,
                "artifacts": baseline_artifacts,
                "version": "1.2.2~acceptance.1+gbbbbbbbbbbbb",
            }
        )
    )
    return objects, candidate, baseline, common


def _graph_command(objects: Path, candidate: Path, baseline: Path) -> list[object]:
    return [
        sys.executable,
        ENTRY_POINT,
        "check-publication-graph",
        "--candidate-release",
        candidate,
        "--baseline-release",
        baseline,
        "--object-root",
        objects,
        "--channel",
        "dev",
        "--version",
        "1.2.3",
        "--source-sha",
        SOURCE_SHA,
        "--generation",
        GENERATION,
        "--platform",
        "linux-amd64",
    ]


def test_publication_graph_binds_both_native_candidate_and_baseline_packages(
    tmp_path: Path,
) -> None:
    """Dropping either architecture from either immutable graph must break the gate."""
    objects, candidate, baseline, common = _graph_inputs(tmp_path)

    result = subprocess.run(
        _graph_command(objects, candidate, baseline),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "baseline_package_sha256": hashlib.sha256(
            b"baseline-linux-amd64"
        ).hexdigest(),
        "baseline_version": "1.2.2~acceptance.1+gbbbbbbbbbbbb",
        "candidate_package_sha256": hashlib.sha256(
            b"candidate-linux-amd64"
        ).hexdigest(),
        "candidate_version": "1.2.3",
        "channel": "dev",
        "generation": GENERATION,
        "images_sha256": hashlib.sha256(
            json.dumps(
                common["images"], sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
        ).hexdigest(),
        "packages": {
            "linux-amd64": {
                "baseline_sha256": hashlib.sha256(
                    b"baseline-linux-amd64"
                ).hexdigest(),
                "candidate_sha256": hashlib.sha256(
                    b"candidate-linux-amd64"
                ).hexdigest(),
            },
            "linux-arm64": {
                "baseline_sha256": hashlib.sha256(
                    b"baseline-linux-arm64"
                ).hexdigest(),
                "candidate_sha256": hashlib.sha256(
                    b"candidate-linux-arm64"
                ).hexdigest(),
            },
        },
        "platform": "linux-amd64",
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "verified_platforms": ["linux-amd64", "linux-arm64"],
    }


def test_publication_graph_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    objects, candidate, baseline, _ = _graph_inputs(tmp_path)
    artifacts = objects / "artifacts"
    real_artifacts = objects / "real-artifacts"
    artifacts.rename(real_artifacts)
    artifacts.symlink_to(real_artifacts, target_is_directory=True)

    result = subprocess.run(
        _graph_command(objects, candidate, baseline),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unsafe" in result.stderr or "unavailable" in result.stderr


def test_publication_graph_rejects_symlinked_artifact_file(tmp_path: Path) -> None:
    objects, candidate, baseline, _ = _graph_inputs(tmp_path)
    artifact = (
        objects
        / f"artifacts/dev/releases/{GENERATION}/spark/current/linux-amd64/vonk-forge-agent.deb"
    )
    target = artifact.with_name("candidate-real.deb")
    artifact.rename(target)
    artifact.symlink_to(target.name)

    result = subprocess.run(
        _graph_command(objects, candidate, baseline),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unsafe" in result.stderr or "unavailable" in result.stderr


def test_publication_graph_rejects_hardlinked_artifact_file(tmp_path: Path) -> None:
    objects, candidate, baseline, _ = _graph_inputs(tmp_path)
    artifact = (
        objects
        / f"artifacts/dev/releases/{GENERATION}/spark/current/linux-amd64/vonk-forge-agent.deb"
    )
    os.link(artifact, artifact.with_name("ambiguous-link.deb"))

    result = subprocess.run(
        _graph_command(objects, candidate, baseline),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unsafe" in result.stderr


@pytest.mark.parametrize("release_name", ["candidate", "baseline"])
@pytest.mark.parametrize("platform", ["linux-amd64", "linux-arm64"])
def test_publication_graph_rejects_any_missing_native_package_record(
    tmp_path: Path, release_name: str, platform: str
) -> None:
    objects, candidate, baseline, _ = _graph_inputs(tmp_path)
    release_path = {"candidate": candidate, "baseline": baseline}[release_name]
    release = json.loads(release_path.read_text())
    release["artifacts"].pop(f"agent-package-{platform}")
    release_path.write_text(json.dumps(release))

    result = subprocess.run(
        _graph_command(objects, candidate, baseline),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"agent-package-{platform}" in result.stderr


def test_verified_artifact_hashes_open_descriptor_during_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects, candidate_path, _, _ = _graph_inputs(tmp_path)
    candidate = json.loads(candidate_path.read_text())
    expected_path = (
        f"artifacts/dev/releases/{GENERATION}/spark/current/"
        "linux-amd64/vonk-forge-agent.deb"
    )
    artifact = objects / expected_path
    replacement = artifact.with_name("replacement.deb")
    replacement.write_bytes(b"substitute-linux-amd64")
    specification = importlib.util.spec_from_file_location(
        "spark_lifecycle_descriptor_test", ENTRY_POINT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    original_read = os.read
    substituted = False

    def substitute_after_open(descriptor: int, size: int) -> bytes:
        nonlocal substituted
        if not substituted:
            os.replace(replacement, artifact)
            substituted = True
        return original_read(descriptor, size)

    monkeypatch.setattr(module.os, "read", substitute_after_open)

    with pytest.raises(module.LifecycleError, match="changed while read"):
        module._verified_artifact(
            candidate,
            object_root=objects,
            key="agent-package-linux-amd64",
            expected_path=expected_path,
            label="candidate release",
        )

    assert substituted


def test_report_is_emitted_only_from_complete_generation_bound_evidence(
    tmp_path: Path,
) -> None:
    """An unconditional workflow report writer must not be able to pass Spark."""
    evidence = tmp_path / "evidence.json"
    report = tmp_path / "report.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "dev",
                "version": "1.2.3",
                "source_sha": SOURCE_SHA,
                "generation": GENERATION,
                "run_id": 42,
                "platform": "linux-arm64",
                "completed_phases": ARM64_PHASES,
                "proof": _arm64_proof(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            ENTRY_POINT,
            "emit-report",
            "--evidence",
            evidence,
            "--output",
            report,
            "--channel",
            "dev",
            "--version",
            "1.2.3",
            "--source-sha",
            SOURCE_SHA,
            "--generation",
            GENERATION,
            "--run-id",
            "42",
            "--platform",
            "linux-arm64",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert json.loads(report.read_text()) == {
        "channel": "dev",
        "gates": ["spark_arm64", "spark_job", "spark_renewal", "spark_upgrade"],
        "generation": GENERATION,
        "lifecycle": {
            "completed_phases": ARM64_PHASES,
            "proof": _arm64_proof(),
        },
        "platform": "linux-arm64",
        "run_id": 42,
        "schema_version": 2,
        "source_sha": SOURCE_SHA,
        "status": "passed",
        "version": "1.2.3",
    }
