#!/usr/bin/env python3
"""Executable, fail-closed Spark lifecycle acceptance entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PLATFORMS = ("linux-amd64", "linux-arm64")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
CHANNEL = re.compile(r"(?:dev|stable)\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[+~][0-9A-Za-z.+~-]+)?\Z")
BASELINE_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"~acceptance\.1\+g[0-9a-f]{12}\Z"
)
PINNED_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9._-]*@sha256:[0-9a-f]{64}\Z"
)
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
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
CANARY_STATES = [
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
]
REQUIRED_PROOFS = {
    "binary_digest_changed",
    "build_digest_changed",
    "canary_completed_states",
    "certificate_serial_changed",
    "config_preserved",
    "controller_generation",
    "direct_agent_healthy",
    "node_id",
    "node_id_preserved_after_renewal",
    "old_certificate_recorded",
    "old_certificate_rejected",
    "package_digest_changed",
    "pairing_grant_use_count",
    "private_identity_preserved",
    "semantic_version_changed",
    "synthetic_device",
    "verified_platforms",
}


class LifecycleError(RuntimeError):
    """A bounded acceptance failure that contains no credential material."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LifecycleError(f"{label} is invalid")
    return value


def _read_document(path: Path, label: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleError(f"{label} is unavailable or invalid") from error
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
        raise LifecycleError(f"{label} is unsafe")
    return _object(document, label)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_canonical_document(path: Path, label: str) -> dict[str, object]:
    document = _read_document(path, label)
    if path.read_bytes() != _canonical(document):
        raise LifecycleError(f"{label} is not canonical JSON")
    return document


def _atomic_write(path: Path, value: object) -> None:
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise LifecycleError("report directory is unavailable") from error
    if parent.is_symlink() or not parent.is_dir() or metadata.st_nlink < 1:
        raise LifecycleError("report directory is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _assert_release_identity(
    release: dict[str, object],
    *,
    label: str,
    channel: str,
    generation: str,
    source_sha: str,
) -> None:
    if (
        release.get("schema_version") != 1
        or release.get("channel") != channel
        or release.get("generation") != generation
        or release.get("source_sha") != source_sha
    ):
        raise LifecycleError(f"{label} identity does not match this acceptance run")
    images = _object(release.get("images"), f"{label} image graph")
    if not images or any(
        not isinstance(name, str)
        or not name
        or not isinstance(image, str)
        or PINNED_IMAGE.fullmatch(image) is None
        or any(pointer in image for pointer in (":latest@", ":dev@", ":main@", ":edge@"))
        for name, image in images.items()
    ):
        raise LifecycleError(f"{label} image graph is not immutable")


def _verified_artifact(
    release: dict[str, object],
    *,
    object_root: Path,
    key: str,
    expected_path: str,
    label: str,
) -> str:
    artifacts = _object(release.get("artifacts"), f"{label} artifacts")
    record = _object(artifacts.get(key), f"{label} {key}")
    digest = record.get("sha256")
    size = record.get("size")
    relative = record.get("path")
    if (
        relative != expected_path
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        raise LifecycleError(f"{label} {key} record is invalid")
    root = object_root.resolve()
    path = (root / expected_path).resolve()
    try:
        path.relative_to(root)
        metadata = path.lstat()
    except (OSError, ValueError) as error:
        raise LifecycleError(f"{label} {key} object is unavailable") from error
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1 or metadata.st_size != size:
        raise LifecycleError(f"{label} {key} object is unsafe or has the wrong size")
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != digest:
        raise LifecycleError(f"{label} {key} object digest does not match")
    return digest


def check_publication_graph(arguments: argparse.Namespace) -> dict[str, object]:
    if (
        CHANNEL.fullmatch(arguments.channel) is None
        or VERSION.fullmatch(arguments.version) is None
        or SOURCE_SHA.fullmatch(arguments.source_sha) is None
        or SHA256.fullmatch(arguments.generation) is None
        or arguments.platform not in PLATFORMS
    ):
        raise LifecycleError("publication graph inputs are invalid")
    candidate = _read_document(arguments.candidate_release, "candidate release")
    baseline = _read_document(arguments.baseline_release, "baseline release")
    _assert_release_identity(
        candidate,
        label="candidate release",
        channel=arguments.channel,
        generation=arguments.generation,
        source_sha=arguments.source_sha,
    )
    _assert_release_identity(
        baseline,
        label="baseline release",
        channel=arguments.channel,
        generation=arguments.generation,
        source_sha=arguments.source_sha,
    )
    baseline_version = baseline.get("version")
    if (
        candidate.get("version") != arguments.version
        or not isinstance(baseline_version, str)
        or BASELINE_VERSION.fullmatch(baseline_version) is None
        or baseline.get("acceptance_only") is not True
        or baseline.get("images") != candidate.get("images")
    ):
        raise LifecycleError("candidate and baseline release graphs are inconsistent")
    ordered = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", baseline_version, "lt", arguments.version],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ordered.returncode != 0:
        raise LifecycleError("baseline version is not strictly lower than candidate")

    selected: dict[str, str] = {}
    for platform in PLATFORMS:
        candidate_digest = _verified_artifact(
            candidate,
            object_root=arguments.object_root,
            key=f"agent-package-{platform}",
            expected_path=(
                f"artifacts/{arguments.channel}/releases/{arguments.generation}/"
                f"spark/current/{platform}/vonk-forge-agent.deb"
            ),
            label="candidate release",
        )
        baseline_digest = _verified_artifact(
            baseline,
            object_root=arguments.object_root,
            key=f"agent-package-{platform}",
            expected_path=(
                f"artifacts/{arguments.channel}/releases/{arguments.generation}/"
                f"acceptance-baseline/spark/current/{platform}/vonk-forge-agent.deb"
            ),
            label="baseline release",
        )
        if candidate_digest == baseline_digest:
            raise LifecycleError(f"{platform} candidate and baseline package digests match")
        if platform == arguments.platform:
            selected = {
                "baseline_package_sha256": baseline_digest,
                "candidate_package_sha256": candidate_digest,
            }
    return {
        **selected,
        "baseline_version": baseline_version,
        "candidate_version": arguments.version,
        "generation": arguments.generation,
        "platform": arguments.platform,
        "verified_platforms": list(PLATFORMS),
    }


def emit_report(arguments: argparse.Namespace) -> None:
    if (
        CHANNEL.fullmatch(arguments.channel) is None
        or VERSION.fullmatch(arguments.version) is None
        or SOURCE_SHA.fullmatch(arguments.source_sha) is None
        or SHA256.fullmatch(arguments.generation) is None
        or arguments.run_id <= 0
        or arguments.platform not in PLATFORMS
    ):
        raise LifecycleError("report identity is invalid")
    evidence = _read_canonical_document(arguments.evidence, "lifecycle evidence")
    if set(evidence) != {
        "channel",
        "completed_phases",
        "generation",
        "platform",
        "proof",
        "run_id",
        "schema_version",
        "source_sha",
        "version",
    } or any(
        evidence.get(name) != expected
        for name, expected in {
            "schema_version": 1,
            "channel": arguments.channel,
            "version": arguments.version,
            "source_sha": arguments.source_sha,
            "generation": arguments.generation,
            "run_id": arguments.run_id,
            "platform": arguments.platform,
            "completed_phases": PHASES,
        }.items()
    ):
        raise LifecycleError("lifecycle evidence is incomplete or belongs to another run")
    proof = _object(evidence.get("proof"), "lifecycle proof")
    if set(proof) != REQUIRED_PROOFS:
        raise LifecycleError("lifecycle proof is incomplete")
    booleans = REQUIRED_PROOFS - {
        "canary_completed_states",
        "controller_generation",
        "node_id",
        "pairing_grant_use_count",
        "synthetic_device",
        "verified_platforms",
    }
    if (
        any(proof.get(name) is not True for name in booleans)
        or proof.get("verified_platforms") != list(PLATFORMS)
        or proof.get("controller_generation") != arguments.generation
        or not isinstance(proof.get("node_id"), str)
        or NODE_ID.fullmatch(str(proof.get("node_id"))) is None
        or proof.get("pairing_grant_use_count") != 1
        or proof.get("canary_completed_states") != CANARY_STATES
    ):
        raise LifecycleError("lifecycle proof did not satisfy every required assertion")
    synthetic = _object(proof.get("synthetic_device"), "synthetic device proof")
    if (
        set(synthetic)
        != {
            "architecture",
            "cdi_name",
            "fixture_sha256",
            "physical_gpu",
            "provenance",
            "synthetic",
        }
        or synthetic.get("architecture") != arguments.platform
        or synthetic.get("cdi_name") != "nvidia.com/gpu=all"
        or not isinstance(synthetic.get("fixture_sha256"), str)
        or SHA256.fullmatch(str(synthetic.get("fixture_sha256"))) is None
        or synthetic.get("physical_gpu") is not False
        or synthetic.get("provenance") != "ci-only-synthetic-cdi"
        or synthetic.get("synthetic") is not True
    ):
        raise LifecycleError("synthetic device provenance is invalid")
    gates = {
        "linux-amd64": ["spark_amd64", "spark_job", "spark_pairing"],
        "linux-arm64": ["spark_arm64", "spark_renewal", "spark_upgrade"],
    }[arguments.platform]
    _atomic_write(
        arguments.output,
        {
            "channel": arguments.channel,
            "gates": gates,
            "generation": arguments.generation,
            "platform": arguments.platform,
            "run_id": arguments.run_id,
            "schema_version": 2,
            "source_sha": arguments.source_sha,
            "status": "passed",
            "synthetic_device": synthetic,
            "version": arguments.version,
        },
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    graph = commands.add_parser("check-publication-graph")
    graph.add_argument("--candidate-release", type=Path, required=True)
    graph.add_argument("--baseline-release", type=Path, required=True)
    graph.add_argument("--object-root", type=Path, required=True)
    graph.add_argument("--channel", required=True)
    graph.add_argument("--version", required=True)
    graph.add_argument("--source-sha", required=True)
    graph.add_argument("--generation", required=True)
    graph.add_argument("--platform", required=True)
    report = commands.add_parser("emit-report")
    report.add_argument("--evidence", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--channel", required=True)
    report.add_argument("--version", required=True)
    report.add_argument("--source-sha", required=True)
    report.add_argument("--generation", required=True)
    report.add_argument("--run-id", type=int, required=True)
    report.add_argument("--platform", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        if arguments.command == "check-publication-graph":
            result = check_publication_graph(arguments)
        elif arguments.command == "emit-report":
            emit_report(arguments)
            return 0
        else:
            raise LifecycleError("lifecycle command is invalid")
    except LifecycleError as error:
        print(f"Spark lifecycle failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
