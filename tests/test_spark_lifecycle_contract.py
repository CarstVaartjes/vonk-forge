from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = ROOT / "tests/acceptance/test_spark_lifecycle.py"
GENERATION = "a" * 64
SOURCE_SHA = "b" * 40
PHASES = [
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


def _record(root: Path, relative: str, content: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": relative,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def test_publication_graph_binds_both_native_candidate_and_baseline_packages(
    tmp_path: Path,
) -> None:
    """Dropping either architecture from either immutable graph must break the gate."""
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
        json.dumps(
            common
            | {
                "artifacts": candidate_artifacts,
                "version": "1.2.3",
            }
        )
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

    result = subprocess.run(
        [
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
        ],
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
        "generation": GENERATION,
        "platform": "linux-amd64",
        "verified_platforms": ["linux-amd64", "linux-arm64"],
    }


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
                "platform": "linux-amd64",
                "completed_phases": PHASES,
                "proof": {
                    "verified_platforms": ["linux-amd64", "linux-arm64"],
                    "controller_generation": GENERATION,
                    "node_id": "spk_0123456789abcdef0123456789abcdef",
                    "pairing_grant_use_count": 1,
                    "canary_completed_states": [
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
                    "old_certificate_recorded": True,
                    "old_certificate_rejected": True,
                    "certificate_serial_changed": True,
                    "node_id_preserved_after_renewal": True,
                    "semantic_version_changed": True,
                    "package_digest_changed": True,
                    "build_digest_changed": True,
                    "binary_digest_changed": True,
                    "config_preserved": True,
                    "private_identity_preserved": True,
                    "direct_agent_healthy": True,
                    "synthetic_device": {
                        "architecture": "linux-amd64",
                        "cdi_name": "nvidia.com/gpu=all",
                        "fixture_sha256": "e" * 64,
                        "physical_gpu": False,
                        "provenance": "ci-only-synthetic-cdi",
                        "synthetic": True,
                    },
                },
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
            "linux-amd64",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert json.loads(report.read_text()) == {
        "channel": "dev",
        "gates": ["spark_amd64", "spark_job", "spark_pairing"],
        "generation": GENERATION,
        "platform": "linux-amd64",
        "run_id": 42,
        "schema_version": 2,
        "source_sha": SOURCE_SHA,
        "status": "passed",
        "synthetic_device": {
            "architecture": "linux-amd64",
            "cdi_name": "nvidia.com/gpu=all",
            "fixture_sha256": "e" * 64,
            "physical_gpu": False,
            "provenance": "ci-only-synthetic-cdi",
            "synthetic": True,
        },
        "version": "1.2.3",
    }
