"""Strict schema-2 Spark lifecycle report contract shared by emitter and signer."""

from __future__ import annotations

import re
from typing import Any

PLATFORMS = ("linux-amd64", "linux-arm64")
GATES = {
    "linux-amd64": ["spark_amd64", "spark_pairing"],
    "linux-arm64": ["spark_arm64", "spark_job", "spark_renewal", "spark_upgrade"],
}
PHASES = {
    "linux-amd64": [
        "publication-graph-verified",
        "controller-ready",
        "baseline-installed",
        "paired",
        "direct-rust-agent-healthy",
    ],
    "linux-arm64": [
        "publication-graph-verified",
        "controller-ready",
        "baseline-installed",
        "paired",
        "synthetic-device-ready",
        "canary-completed",
        "identity-renewed",
        "candidate-upgraded",
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
        "package_sha256": selected["baseline_sha256"],
        "version": graph["baseline_version"],
    }:
        raise ContractError("AMD64 installation does not match the baseline graph")
    if not isinstance(proof.get("node_id"), str) or NODE_ID.fullmatch(proof["node_id"]) is None:
        raise ContractError("AMD64 node identity proof is invalid")


def _validate_arm64(proof: dict[str, Any], graph: dict[str, Any]) -> None:
    _exact(
        proof,
        {
            "canary",
            "config_sha256_after_upgrade",
            "config_sha256_before_upgrade",
            "controller_generation",
            "direct_agent_health",
            "installation",
            "node_id_after_renewal",
            "node_id_after_upgrade",
            "node_id_before_renewal",
            "pairing_grant_use_count",
            "private_identity_sha256_after_upgrade",
            "private_identity_sha256_before_upgrade",
            "publication_graph",
            "renewal",
            "synthetic_device",
        },
        "ARM64 lifecycle proof",
    )
    node_ids = [
        proof.get("node_id_before_renewal"),
        proof.get("node_id_after_renewal"),
        proof.get("node_id_after_upgrade"),
    ]
    if (
        not all(isinstance(node_id, str) and NODE_ID.fullmatch(node_id) for node_id in node_ids)
        or len(set(node_ids)) != 1
    ):
        raise ContractError("ARM64 node identity was not preserved")

    installation = _object(proof.get("installation"), "ARM64 installation proof")
    _exact(installation, {"architecture", "baseline", "candidate"}, "ARM64 installation proof")
    if installation.get("architecture") != "arm64":
        raise ContractError("ARM64 installation architecture is invalid")
    packages = graph["packages"]["linux-arm64"]
    baseline = _validate_install_identity(
        installation.get("baseline"),
        label="ARM64 baseline installation identity",
        version=graph["baseline_version"],
        package_digest=packages["baseline_sha256"],
    )
    candidate = _validate_install_identity(
        installation.get("candidate"),
        label="ARM64 candidate installation identity",
        version=graph["candidate_version"],
        package_digest=packages["candidate_sha256"],
    )
    if any(
        baseline[field] == candidate[field]
        for field in ("version", "package_sha256", "build_sha256", "binary_sha256")
    ):
        raise ContractError("ARM64 upgraded identities did not all change")

    for before, after, label in (
        ("config_sha256_before_upgrade", "config_sha256_after_upgrade", "config"),
        (
            "private_identity_sha256_before_upgrade",
            "private_identity_sha256_after_upgrade",
            "private identity",
        ),
    ):
        before_digest = _digest(proof.get(before), f"ARM64 {label} before upgrade")
        after_digest = _digest(proof.get(after), f"ARM64 {label} after upgrade")
        if before_digest != after_digest:
            raise ContractError(f"ARM64 {label} was not preserved")

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
