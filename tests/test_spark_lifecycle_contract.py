from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import spark_lifecycle_contract

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = ROOT / "tests/acceptance/test_spark_lifecycle.py"
GENERATION = "a" * 64
SOURCE_SHA = "b" * 40
ARM64_PHASES = [
    "publication-graph-verified",
    "controller-ready",
    "candidate-installed",
    "paired",
    "synthetic-device-ready",
    "canary-completed",
    "identity-renewed",
    "direct-rust-agent-healthy",
]
AMD64_PHASES = [
    "publication-graph-verified",
    "controller-ready",
    "candidate-installed",
    "paired",
    "synthetic-device-ready",
    "canary-completed",
    "direct-rust-agent-healthy",
]


def _acceptance_module():
    specification = importlib.util.spec_from_file_location(
        "spark_lifecycle_acceptance", ENTRY_POINT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


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
        "controller_generation": GENERATION,
        "direct_agent_health": {
            "healthy": True,
            "implementation": "rust",
            "transport": "direct",
        },
        "installation": {
            "architecture": "arm64",
            "identity": {
                "binary_sha256": "9" * 64,
                "build_sha256": "a" * 64,
                "package_sha256": "4" * 64,
                "version": "1.2.3",
            },
        },
        "node_id_after_renewal": node_id,
        "node_id_before_renewal": node_id,
        "pairing_grant_use_count": 1,
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


def _amd64_proof() -> dict[str, object]:
    graph = _publication_graph()
    graph["platform"] = "linux-amd64"
    graph["baseline_package_sha256"] = "1" * 64
    graph["candidate_package_sha256"] = "2" * 64
    return {
        "canary": {
            "completed_states": spark_lifecycle_contract.CANARY_STATES,
            "deterministic_response_sha256": "5" * 64,
        },
        "controller_generation": GENERATION,
        "direct_agent_health": {
            "healthy": True,
            "implementation": "rust",
            "transport": "direct",
        },
        "installation": {
            "architecture": "amd64",
            "package_sha256": "2" * 64,
            "version": "1.2.3",
        },
        "node_id": "spk_0123456789abcdef0123456789abcdef",
        "pairing_grant_use_count": 1,
        "publication_graph": graph,
        "synthetic_device": {
            "architecture": "linux-amd64",
            "cdi_name": "nvidia.com/gpu=all",
            "fixture_sha256": "e" * 64,
            "physical_gpu": False,
            "provenance": "ci-only-synthetic-cdi",
            "synthetic": True,
        },
    }


@pytest.mark.parametrize(
    ("platform", "phases", "proof"),
    [
        ("linux-amd64", AMD64_PHASES, _amd64_proof),
        ("linux-arm64", ARM64_PHASES, _arm64_proof),
    ],
)
def test_each_architecture_requires_the_same_executable_canary(
    platform: str,
    phases: list[str],
    proof,
) -> None:
    value = {"completed_phases": phases, "proof": proof()}

    spark_lifecycle_contract.validate_lifecycle(
        value,
        platform=platform,
        channel="dev",
        version="1.2.3",
        source_sha=SOURCE_SHA,
        generation=GENERATION,
    )

    del value["proof"]["canary"]
    with pytest.raises(
        spark_lifecycle_contract.ContractError,
        match=f"{platform.removeprefix('linux-').upper()} lifecycle proof",
    ):
        spark_lifecycle_contract.validate_lifecycle(
            value,
            platform=platform,
            channel="dev",
            version="1.2.3",
            source_sha=SOURCE_SHA,
            generation=GENERATION,
        )


def _record(root: Path, relative: str, content: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": relative,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _write_canonical(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")


def _graph_inputs(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    objects = tmp_path / "objects"
    candidate_artifacts: dict[str, dict[str, object]] = {}
    for platform in ("linux-amd64", "linux-arm64"):
        package_path = (
            f"artifacts/dev/releases/{GENERATION}/spark/current/{platform}/"
            "vonk-forge-agent.deb"
        )
        candidate_artifacts[f"agent-package-{platform}"] = _record(
            objects,
            package_path,
            f"candidate-{platform}".encode(),
        ) | {
            "architecture": platform,
            "host_signature": "e" * 128,
            "package_version": "1.2.3",
            "target_binary_digest": "f" * 64,
            "target_build_digest": "sha256:" + "d" * 64,
        }
        candidate_artifacts[f"agent-package-signature-{platform}"] = _record(
            objects,
            f"{package_path}.host.sig",
            ("e" * 128 + "\n").encode(),
        )
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
        "schema_version": 2,
        "source_sha": SOURCE_SHA,
    }
    candidate = objects / f"artifacts/dev/releases/{GENERATION}/release.json"
    _write_canonical(
        candidate,
        common
        | {
            "artifacts": candidate_artifacts,
            "bootstraps": {},
            "version": "1.2.3",
        },
    )
    baseline = (
        objects
        / f"artifacts/dev/releases/{GENERATION}/acceptance-baseline/release.json"
    )
    _write_canonical(
        baseline,
        common
        | {
            "acceptance_only": True,
            "artifacts": baseline_artifacts,
            "bootstraps": {},
            "version": "1.2.2~acceptance.1+gbbbbbbbbbbbb",
        },
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
        "baseline_package_sha256": hashlib.sha256(b"baseline-linux-amd64").hexdigest(),
        "baseline_version": "1.2.2~acceptance.1+gbbbbbbbbbbbb",
        "candidate_package_sha256": hashlib.sha256(
            b"candidate-linux-amd64"
        ).hexdigest(),
        "candidate_version": "1.2.3",
        "channel": "dev",
        "generation": GENERATION,
        "images_sha256": hashlib.sha256(
            json.dumps(common["images"], sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        ).hexdigest(),
        "packages": {
            "linux-amd64": {
                "baseline_sha256": hashlib.sha256(b"baseline-linux-amd64").hexdigest(),
                "candidate_sha256": hashlib.sha256(
                    b"candidate-linux-amd64"
                ).hexdigest(),
            },
            "linux-arm64": {
                "baseline_sha256": hashlib.sha256(b"baseline-linux-arm64").hexdigest(),
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
    _write_canonical(release_path, release)

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
    objects, candidate_path, baseline_path, _ = _graph_inputs(tmp_path)
    expected_path = (
        f"artifacts/dev/releases/{GENERATION}/spark/current/"
        "linux-amd64/vonk-forge-agent.deb"
    )
    artifact = objects / expected_path
    replacement = artifact.with_name("replacement.deb")
    replacement.write_bytes(b"substitute-linux-amd64")
    original_read = os.read
    substituted = False

    def substitute_after_open(descriptor: int, size: int) -> bytes:
        nonlocal substituted
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not substituted and descriptor_path == artifact:
            os.replace(replacement, artifact)
            substituted = True
        return original_read(descriptor, size)

    monkeypatch.setattr(spark_lifecycle_contract.os, "read", substitute_after_open)

    with pytest.raises(
        spark_lifecycle_contract.ContractError, match="changed while read"
    ):
        spark_lifecycle_contract.recompute_publication_graphs(
            candidate_release=candidate_path,
            baseline_release=baseline_path,
            object_root=objects,
            channel="dev",
            version="1.2.3",
            source_sha=SOURCE_SHA,
            generation=GENERATION,
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
        "gates": ["spark_arm64", "spark_job", "spark_renewal"],
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


def _run_arguments(
    objects: Path, candidate: Path, baseline: Path, output: Path
) -> Namespace:
    return Namespace(
        candidate_release=candidate,
        baseline_release=baseline,
        object_root=objects,
        output=output,
        channel="dev",
        version="1.2.3",
        source_sha=SOURCE_SHA,
        generation=GENERATION,
        run_id=42,
        platform="linux-arm64",
    )


def test_run_owns_observation_validation_cleanup_and_report_emission(
    tmp_path: Path,
) -> None:
    """Removing observation or cleanup must make the real run fail closed."""
    acceptance = _acceptance_module()
    objects, candidate, baseline, _ = _graph_inputs(tmp_path)
    report = tmp_path / "report.json"
    events: list[str] = []

    class ObservedLifecycle:
        def __init__(self, graph: dict[str, object]) -> None:
            proof = _arm64_proof()
            proof["publication_graph"] = graph
            installation = proof["installation"]
            assert isinstance(installation, dict)
            identity = installation["identity"]
            assert isinstance(identity, dict)
            identity["package_sha256"] = graph["candidate_package_sha256"]
            self.proof = proof

        def __enter__(self):
            events.append("controller-started")
            return self

        def observe(self) -> dict[str, object]:
            events.append("lifecycle-observed")
            return self.proof

        def __exit__(self, *_error: object) -> None:
            events.append("controller-removed-with-volumes")

    acceptance.run_lifecycle(
        _run_arguments(objects, candidate, baseline, report),
        lifecycle_factory=lambda _arguments, graph: ObservedLifecycle(graph),
    )

    assert events == [
        "controller-started",
        "lifecycle-observed",
        "controller-removed-with-volumes",
    ]
    emitted = json.loads(report.read_text())
    assert emitted["schema_version"] == 2
    assert emitted["status"] == "passed"
    assert emitted["lifecycle"]["completed_phases"] == ARM64_PHASES
    assert emitted["lifecycle"]["proof"]["publication_graph"]["generation"] == (
        GENERATION
    )


def test_run_failure_removes_controller_volumes_without_emitting_report(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance_module()
    objects, candidate, baseline, _ = _graph_inputs(tmp_path)
    report = tmp_path / "report.json"
    events: list[str] = []

    class FailedLifecycle:
        def __enter__(self):
            events.append("controller-started")
            return self

        def observe(self) -> dict[str, object]:
            events.append("lifecycle-failed")
            raise acceptance.LifecycleError("observed lifecycle failed")

        def __exit__(self, *_error: object) -> None:
            events.append("controller-removed-with-volumes")

    with pytest.raises(acceptance.LifecycleError, match="observed lifecycle failed"):
        acceptance.run_lifecycle(
            _run_arguments(objects, candidate, baseline, report),
            lifecycle_factory=lambda _arguments, _graph: FailedLifecycle(),
        )

    assert events == [
        "controller-started",
        "lifecycle-failed",
        "controller-removed-with-volumes",
    ]
    assert not report.exists()
