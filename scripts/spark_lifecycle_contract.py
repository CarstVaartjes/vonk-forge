"""Strict schema-2 Spark lifecycle report contract shared by emitter and signer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

PLATFORMS = ("linux-amd64", "linux-arm64")
GATES = {
    "linux-amd64": ["spark_amd64", "spark_pairing"],
    "linux-arm64": ["spark_arm64", "spark_job", "spark_renewal"],
}
PHASES = {
    "linux-amd64": [
        "publication-graph-verified",
        "controller-ready",
        "candidate-installed",
        "paired",
        "direct-rust-agent-healthy",
    ],
    "linux-arm64": [
        "publication-graph-verified",
        "controller-ready",
        "candidate-installed",
        "paired",
        "synthetic-device-ready",
        "canary-completed",
        "identity-renewed",
        "direct-rust-agent-healthy",
    ],
}
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
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
SERIAL = re.compile(r"[0-9a-f]{16,64}\Z")
VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:[+~][0-9A-Za-z.+~-]+)?\Z"
)
BASELINE_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"~acceptance\.1\+g[0-9a-f]{12}\Z"
)
PINNED_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9._-]*"
    r"@sha256:[0-9a-f]{64}\Z"
)
MAX_RELEASE_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024


class ContractError(ValueError):
    """Lifecycle proof does not satisfy the signing contract."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} is invalid")
    return value


def _exact(value: dict[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise ContractError(f"{label} is incomplete")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _safe_object_bytes(
    object_root: Path,
    relative_path: str,
    *,
    label: str,
    maximum: int,
    expected_size: int | None = None,
    expected_digest: str | None = None,
    capture_content: bool = True,
) -> tuple[bytes | None, str]:
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    root = Path(os.path.abspath(os.fspath(object_root)))
    relative = Path(relative_path)
    relative_parts = relative.parts
    if (
        not root.is_absolute()
        or relative.is_absolute()
        or not relative_parts
        or any(part in {"", ".", ".."} for part in relative_parts)
    ):
        raise ContractError(f"{label} path is unsafe")
    directory = -1
    descriptor = -1
    try:
        directory = os.open(os.sep, directory_flags)
        for component in (*root.parts[1:], *relative_parts[:-1]):
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative_parts[-1], file_flags, dir_fd=directory)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
            or (expected_size is not None and before.st_size != expected_size)
        ):
            raise ContractError(f"{label} is unsafe or has the wrong size")
        content = bytearray() if capture_content else None
        digest_builder = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ContractError(f"{label} changed while read")
            if content is not None:
                content.extend(chunk)
            digest_builder.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if any(
            getattr(before, field) != getattr(after, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        ):
            raise ContractError(f"{label} changed while read")
        observed_digest = digest_builder.hexdigest()
    except OSError as error:
        raise ContractError(f"{label} is unavailable or unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)
    if expected_digest is not None and observed_digest != expected_digest:
        raise ContractError(f"{label} digest does not match its release record")
    return bytes(content) if content is not None else None, observed_digest


def _safe_release_document(
    object_root: Path, relative_path: str, label: str
) -> dict[str, Any]:
    raw, _ = _safe_object_bytes(
        object_root,
        relative_path,
        label=label,
        maximum=MAX_RELEASE_BYTES,
    )
    if raw is None:
        raise ContractError(f"{label} is unavailable")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label} is invalid") from error
    if not isinstance(document, dict) or raw != _canonical(document):
        raise ContractError(f"{label} is not canonical JSON")
    return document


def _release_record(
    release: dict[str, Any],
    *,
    key: str,
    expected_path: str,
    label: str,
) -> tuple[int, str]:
    artifacts = _object(release.get("artifacts"), f"{label} artifacts")
    record = _object(artifacts.get(key), f"{label} {key}")
    _exact(record, {"path", "sha256", "size"}, f"{label} {key}")
    size = record.get("size")
    digest = record.get("sha256")
    if (
        record.get("path") != expected_path
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
    ):
        raise ContractError(f"{label} {key} record is invalid")
    return size, digest


def recompute_publication_graphs(
    *,
    candidate_release: Path,
    baseline_release: Path,
    object_root: Path,
    channel: str,
    version: str,
    source_sha: str,
    generation: str,
) -> dict[str, dict[str, Any]]:
    """Verify immutable authority objects and return exact per-platform graphs."""
    release_prefix = f"artifacts/{channel}/releases/{generation}"
    candidate_relative = f"{release_prefix}/release.json"
    baseline_relative = f"{release_prefix}/acceptance-baseline/release.json"
    root = Path(os.path.abspath(os.fspath(object_root)))
    expected_candidate = root / candidate_relative
    expected_baseline = root / baseline_relative
    if (
        Path(os.path.abspath(os.fspath(candidate_release))) != expected_candidate
        or Path(os.path.abspath(os.fspath(baseline_release))) != expected_baseline
    ):
        raise ContractError("publication release paths do not match the candidate generation")
    candidate = _safe_release_document(
        root, candidate_relative, "candidate release object"
    )
    baseline = _safe_release_document(
        root, baseline_relative, "acceptance baseline release object"
    )
    _exact(
        candidate,
        {
            "artifacts",
            "bootstraps",
            "channel",
            "generation",
            "images",
            "schema_version",
            "source_sha",
            "version",
        },
        "candidate release object",
    )
    _exact(
        baseline,
        {
            "acceptance_only",
            "artifacts",
            "bootstraps",
            "channel",
            "generation",
            "images",
            "schema_version",
            "source_sha",
            "version",
        },
        "acceptance baseline release object",
    )
    expected_identity = {
        "channel": channel,
        "generation": generation,
        "schema_version": 1,
        "source_sha": source_sha,
    }
    if (
        any(candidate.get(field) != value for field, value in expected_identity.items())
        or any(baseline.get(field) != value for field, value in expected_identity.items())
        or candidate.get("version") != version
        or baseline.get("acceptance_only") is not True
    ):
        raise ContractError("publication release identity does not match the acceptance run")
    baseline_version = baseline.get("version")
    if not isinstance(baseline_version, str) or BASELINE_VERSION.fullmatch(
        baseline_version
    ) is None:
        raise ContractError("acceptance baseline version is invalid")
    ordered = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", baseline_version, "lt", version],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ordered.returncode != 0:
        raise ContractError("acceptance baseline version is not strictly lower")
    images = _object(candidate.get("images"), "candidate image graph")
    if (
        not images
        or baseline.get("images") != images
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(image, str)
            or PINNED_IMAGE.fullmatch(image) is None
            or any(pointer in image for pointer in (":latest@", ":dev@", ":main@", ":edge@"))
            for name, image in images.items()
        )
    ):
        raise ContractError("publication image graph is not immutable")

    packages: dict[str, dict[str, str]] = {}
    for platform in PLATFORMS:
        candidate_path = (
            f"{release_prefix}/spark/current/{platform}/vonk-forge-agent.deb"
        )
        baseline_path = (
            f"{release_prefix}/acceptance-baseline/spark/current/{platform}/"
            "vonk-forge-agent.deb"
        )
        candidate_size, candidate_digest = _release_record(
            candidate,
            key=f"agent-package-{platform}",
            expected_path=candidate_path,
            label="candidate release object",
        )
        baseline_size, baseline_digest = _release_record(
            baseline,
            key=f"agent-package-{platform}",
            expected_path=baseline_path,
            label="acceptance baseline release object",
        )
        _, observed_candidate = _safe_object_bytes(
            root,
            candidate_path,
            label=f"candidate {platform} package object",
            maximum=MAX_PACKAGE_BYTES,
            expected_size=candidate_size,
            expected_digest=candidate_digest,
            capture_content=False,
        )
        _, observed_baseline = _safe_object_bytes(
            root,
            baseline_path,
            label=f"acceptance baseline {platform} package object",
            maximum=MAX_PACKAGE_BYTES,
            expected_size=baseline_size,
            expected_digest=baseline_digest,
            capture_content=False,
        )
        if observed_candidate == observed_baseline:
            raise ContractError(f"{platform} package identities did not change")
        packages[platform] = {
            "baseline_sha256": observed_baseline,
            "candidate_sha256": observed_candidate,
        }

    common: dict[str, Any] = {
        "baseline_version": baseline_version,
        "candidate_version": version,
        "channel": channel,
        "generation": generation,
        "images_sha256": hashlib.sha256(_canonical(images)).hexdigest(),
        "packages": packages,
        "schema_version": 1,
        "source_sha": source_sha,
        "verified_platforms": list(PLATFORMS),
    }
    return {
        platform: {
            "baseline_package_sha256": packages[platform]["baseline_sha256"],
            "candidate_package_sha256": packages[platform]["candidate_sha256"],
            **common,
            "platform": platform,
        }
        for platform in PLATFORMS
    }


def _version_tuple(value: str, pattern: re.Pattern[str], label: str) -> tuple[int, int, int]:
    match = pattern.fullmatch(value)
    if match is None:
        raise ContractError(f"{label} is invalid")
    return tuple(int(part) for part in match.groups()[:3])


def _validate_graph(
    value: object,
    *,
    platform: str,
    channel: str,
    version: str,
    source_sha: str,
    generation: str,
) -> dict[str, Any]:
    graph = _object(value, "publication graph proof")
    _exact(
        graph,
        {
            "baseline_package_sha256",
            "baseline_version",
            "candidate_package_sha256",
            "candidate_version",
            "channel",
            "generation",
            "images_sha256",
            "packages",
            "platform",
            "schema_version",
            "source_sha",
            "verified_platforms",
        },
        "publication graph proof",
    )
    if (
        graph.get("schema_version") != 1
        or graph.get("channel") != channel
        or graph.get("candidate_version") != version
        or graph.get("source_sha") != source_sha
        or graph.get("generation") != generation
        or graph.get("platform") != platform
        or graph.get("verified_platforms") != list(PLATFORMS)
    ):
        raise ContractError("publication graph proof belongs to another run")
    baseline_version = graph.get("baseline_version")
    if not isinstance(baseline_version, str):
        raise ContractError("publication graph baseline version is invalid")
    baseline_tuple = _version_tuple(
        baseline_version, BASELINE_VERSION, "publication graph baseline version"
    )
    candidate_tuple = _version_tuple(
        version, VERSION, "publication graph candidate version"
    )
    if baseline_tuple >= candidate_tuple:
        raise ContractError("publication graph versions are not strictly ordered")
    _digest(graph.get("images_sha256"), "publication graph image digest")
    packages = _object(graph.get("packages"), "publication graph packages")
    _exact(packages, set(PLATFORMS), "publication graph packages")
    for native_platform in PLATFORMS:
        package = _object(
            packages.get(native_platform),
            f"publication graph {native_platform} package",
        )
        _exact(
            package,
            {"baseline_sha256", "candidate_sha256"},
            f"publication graph {native_platform} package",
        )
        baseline_digest = _digest(
            package.get("baseline_sha256"),
            f"publication graph {native_platform} baseline package",
        )
        candidate_digest = _digest(
            package.get("candidate_sha256"),
            f"publication graph {native_platform} candidate package",
        )
        if baseline_digest == candidate_digest:
            raise ContractError(
                f"publication graph {native_platform} package identities did not change"
            )
    selected = packages[platform]
    if (
        graph.get("baseline_package_sha256") != selected["baseline_sha256"]
        or graph.get("candidate_package_sha256") != selected["candidate_sha256"]
    ):
        raise ContractError("publication graph selected package identities changed")
    return graph


def _validate_direct_agent(value: object) -> None:
    health = _object(value, "direct agent health proof")
    _exact(health, {"healthy", "implementation", "transport"}, "direct agent health proof")
    if health != {
        "healthy": True,
        "implementation": "rust",
        "transport": "direct",
    }:
        raise ContractError("direct Rust agent health proof is invalid")


def _validate_install_identity(
    value: object,
    *,
    label: str,
    version: str,
    package_digest: str,
) -> dict[str, Any]:
    identity = _object(value, label)
    _exact(
        identity,
        {"binary_sha256", "build_sha256", "package_sha256", "version"},
        label,
    )
    if identity.get("version") != version or identity.get("package_sha256") != package_digest:
        raise ContractError(f"{label} does not match the publication graph")
    for field in ("binary_sha256", "build_sha256", "package_sha256"):
        _digest(identity.get(field), f"{label} {field}")
    return identity


def _validate_amd64(proof: dict[str, Any], graph: dict[str, Any]) -> None:
    _exact(
        proof,
        {
            "controller_generation",
            "direct_agent_health",
            "installation",
            "node_id",
            "pairing_grant_use_count",
            "publication_graph",
        },
        "AMD64 lifecycle proof",
    )
    installation = _object(proof.get("installation"), "AMD64 installation proof")
    _exact(
        installation,
        {"architecture", "package_sha256", "version"},
        "AMD64 installation proof",
    )
    selected = graph["packages"]["linux-amd64"]
    if installation != {
        "architecture": "amd64",
        "package_sha256": selected["candidate_sha256"],
        "version": graph["candidate_version"],
    }:
        raise ContractError("AMD64 installation does not match the candidate graph")
    if not isinstance(proof.get("node_id"), str) or NODE_ID.fullmatch(proof["node_id"]) is None:
        raise ContractError("AMD64 node identity proof is invalid")


def _validate_arm64(proof: dict[str, Any], graph: dict[str, Any]) -> None:
    _exact(
        proof,
        {
            "canary",
            "controller_generation",
            "direct_agent_health",
            "installation",
            "node_id_after_renewal",
            "node_id_before_renewal",
            "pairing_grant_use_count",
            "publication_graph",
            "renewal",
            "synthetic_device",
        },
        "ARM64 lifecycle proof",
    )
    node_ids = [
        proof.get("node_id_before_renewal"),
        proof.get("node_id_after_renewal"),
    ]
    if (
        not all(isinstance(node_id, str) and NODE_ID.fullmatch(node_id) for node_id in node_ids)
        or len(set(node_ids)) != 1
    ):
        raise ContractError("ARM64 node identity was not preserved")

    installation = _object(proof.get("installation"), "ARM64 installation proof")
    _exact(installation, {"architecture", "identity"}, "ARM64 installation proof")
    if installation.get("architecture") != "arm64":
        raise ContractError("ARM64 installation architecture is invalid")
    packages = graph["packages"]["linux-arm64"]
    _validate_install_identity(
        installation.get("identity"),
        label="ARM64 candidate installation identity",
        version=graph["candidate_version"],
        package_digest=packages["candidate_sha256"],
    )

    renewal = _object(proof.get("renewal"), "ARM64 renewal proof")
    _exact(
        renewal,
        {
            "certificate_serial_after",
            "certificate_serial_before",
            "old_certificate_rejection",
        },
        "ARM64 renewal proof",
    )
    serial_before = renewal.get("certificate_serial_before")
    serial_after = renewal.get("certificate_serial_after")
    if (
        not isinstance(serial_before, str)
        or SERIAL.fullmatch(serial_before) is None
        or not isinstance(serial_after, str)
        or SERIAL.fullmatch(serial_after) is None
        or serial_before == serial_after
    ):
        raise ContractError("ARM64 certificate serial did not renew")
    rejection = _object(
        renewal.get("old_certificate_rejection"),
        "ARM64 old certificate rejection proof",
    )
    _exact(
        rejection,
        {"durably_recorded", "rejected", "serial"},
        "ARM64 old certificate rejection proof",
    )
    if rejection != {
        "durably_recorded": True,
        "rejected": True,
        "serial": serial_before,
    }:
        raise ContractError("ARM64 old certificate was not durably rejected")

    canary = _object(proof.get("canary"), "ARM64 canary proof")
    _exact(canary, {"completed_states", "deterministic_response_sha256"}, "ARM64 canary proof")
    if canary.get("completed_states") != CANARY_STATES:
        raise ContractError("ARM64 canary phases are incomplete")
    _digest(canary.get("deterministic_response_sha256"), "ARM64 canary response")

    synthetic = _object(proof.get("synthetic_device"), "ARM64 synthetic device proof")
    _exact(
        synthetic,
        {
            "architecture",
            "cdi_name",
            "fixture_sha256",
            "physical_gpu",
            "provenance",
            "synthetic",
        },
        "ARM64 synthetic device proof",
    )
    _digest(synthetic.get("fixture_sha256"), "ARM64 synthetic CDI fixture")
    if synthetic != {
        "architecture": "linux-arm64",
        "cdi_name": "nvidia.com/gpu=all",
        "fixture_sha256": synthetic["fixture_sha256"],
        "physical_gpu": False,
        "provenance": "ci-only-synthetic-cdi",
        "synthetic": True,
    }:
        raise ContractError("ARM64 synthetic CDI provenance is invalid")


def validate_lifecycle(
    value: object,
    *,
    platform: str,
    channel: str,
    version: str,
    source_sha: str,
    generation: str,
    expected_publication_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a validated lifecycle object or fail closed."""
    if platform not in PLATFORMS:
        raise ContractError("Spark lifecycle platform is invalid")
    lifecycle = _object(value, "Spark lifecycle")
    _exact(lifecycle, {"completed_phases", "proof"}, "Spark lifecycle")
    if lifecycle.get("completed_phases") != PHASES[platform]:
        raise ContractError("Spark lifecycle phases are incomplete")
    proof = _object(lifecycle.get("proof"), "Spark lifecycle proof")
    graph = _validate_graph(
        proof.get("publication_graph"),
        platform=platform,
        channel=channel,
        version=version,
        source_sha=source_sha,
        generation=generation,
    )
    if expected_publication_graph is not None and graph != expected_publication_graph:
        raise ContractError(
            "publication graph proof does not match authority publication objects"
        )
    if proof.get("controller_generation") != generation:
        raise ContractError("controller generation does not match the acceptance run")
    if proof.get("pairing_grant_use_count") != 1:
        raise ContractError("pairing grant was not used exactly once")
    _validate_direct_agent(proof.get("direct_agent_health"))
    if platform == "linux-amd64":
        _validate_amd64(proof, graph)
    else:
        _validate_arm64(proof, graph)
    return lifecycle
