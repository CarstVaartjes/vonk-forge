"""Inspection of exact stored OCI image content."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from vonk_agent_protocol import (
    RuntimeImageCode,
    WaitReason,
)

from .contracts import (
    _IMAGE_DIGEST,
    _SHA256,
    PulledImageEvidence,
    RuntimeImagePreparationInvalid,
    RuntimeImagePreparationRefused,
    RuntimeImagePreparationUnknown,
    _runtime_interface_label,
)


class OciLayoutImageTransport:
    """Read a stored image's identity from the layered store, no subprocess.

    ``archive`` is the image's manifest blob in the Controller's OCI layout;
    its config blob sits beside it. The bytes were verified when they entered
    the layout, so this only reads the platform and the interface label.
    """

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        expected_runtime_interface = _runtime_interface_label(
            expected_runtime_interface
        )
        try:
            config_digest = _config_digest(archive.read_text(encoding="utf-8"))
            config = json.loads(
                (archive.parent / config_digest.removeprefix("sha256:")).read_bytes()
            )
        except (OSError, ValueError) as error:
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.INSPECT_INVALID,
                "stored OCI image manifest or config is unreadable",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        if not isinstance(config, Mapping):
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.INSPECT_INVALID,
                "stored OCI image config is invalid",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        architecture = _observed_architecture(config)
        if architecture != expected_architecture:
            raise RuntimeImagePreparationRefused(
                RuntimeImageCode.ARCHITECTURE_MISMATCH,
                "OCI image architecture does not match the recipe",
            )
        interface = _observed_runtime_interface(config)
        if interface != expected_runtime_interface:
            raise RuntimeImagePreparationRefused(
                RuntimeImageCode.INTERFACE_MISMATCH,
                "OCI image runtime interface label does not match the recipe",
            )
        return PulledImageEvidence(
            manifest_digest=f"sha256:{archive.name}",
            config_id=config_digest,
            local_reference=f"oci-layout:{archive.name}",
            architecture=architecture,
            runtime_interface=interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def _validate_evidence(
    evidence: PulledImageEvidence,
    expected_architecture: str,
    expected_interface: str | None,
) -> None:
    if not isinstance(evidence, PulledImageEvidence):
        raise RuntimeImagePreparationInvalid(
            RuntimeImageCode.EVIDENCE_INVALID, "OCI transport returned invalid evidence"
        )
    if _IMAGE_DIGEST.fullmatch(evidence.manifest_digest) is None:
        raise RuntimeImagePreparationRefused(
            RuntimeImageCode.DIGEST_MISMATCH_,
            "OCI transport returned a different manifest digest",
        )
    if (
        _IMAGE_DIGEST.fullmatch(evidence.config_id) is None
        or not evidence.local_reference
    ):
        raise RuntimeImagePreparationInvalid(
            RuntimeImageCode.EVIDENCE_INVALID,
            "OCI transport did not return local image identity",
        )
    if (
        _SHA256.fullmatch(evidence.archive_sha256) is None
        or type(evidence.archive_bytes) is not int
        or evidence.archive_bytes < 1
    ):
        raise RuntimeImagePreparationInvalid(
            RuntimeImageCode.EVIDENCE_INVALID,
            "OCI transport did not return archive verification evidence",
        )
    if evidence.architecture != expected_architecture:
        raise RuntimeImagePreparationRefused(
            RuntimeImageCode.ARCHITECTURE_MISMATCH,
            "verified image architecture does not match the recipe",
        )
    if not evidence.runtime_interface or (
        expected_interface is not None
        and evidence.runtime_interface != expected_interface
    ):
        raise RuntimeImagePreparationRefused(
            RuntimeImageCode.INTERFACE_MISMATCH,
            "verified image runtime interface does not match the recipe",
        )


def _run_json_text(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.INSPECT_INVALID,
            "stored OCI manifest is invalid JSON",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def _observed_architecture(image: Mapping[str, object]) -> str:
    os_name, architecture = (
        image.get("os", image.get("Os")),
        image.get("architecture", image.get("Architecture")),
    )
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        raise RuntimeImagePreparationRefused(
            RuntimeImageCode.ARCHITECTURE_MISSING, "OCI image platform is missing"
        )
    return f"{os_name}/{architecture}"


def _observed_runtime_interface(image: Mapping[str, object]) -> str:
    labels = image.get("config", image.get("Config"))
    if isinstance(labels, Mapping):
        labels = labels.get("Labels", labels.get("labels"))
    if not isinstance(labels, Mapping):
        raise RuntimeImagePreparationRefused(
            RuntimeImageCode.INTERFACE_MISSING,
            "OCI image runtime interface label is missing",
        )
    values = {
        str(labels[name])
        for name in (
            "ai.vonkforge.runtime-interface",
            "com.vonk.runtime.interface",
            "org.opencontainers.image.runtime.interface",
            "org.opencontainers.image.runtime-interface",
        )
        if isinstance(labels.get(name), str) and labels.get(name)
    }
    if len(values) != 1:
        raise RuntimeImagePreparationRefused(
            RuntimeImageCode.INTERFACE_MISSING,
            "OCI image runtime interface label is missing or ambiguous",
        )
    return values.pop()


def _config_digest(raw_manifest: str) -> str:
    value = _run_json_text(raw_manifest)
    config = value.get("config") if isinstance(value, Mapping) else None
    digest = config.get("digest") if isinstance(config, Mapping) else None
    if not isinstance(digest, str) or _IMAGE_DIGEST.fullmatch(digest) is None:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.CONFIG_MISSING,
            "OCI manifest config digest is missing",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return digest
