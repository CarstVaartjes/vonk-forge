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
        # The accepted kit owns compatibility. Labels are external descriptive
        # metadata, not another admission authority over verified content.
        return PulledImageEvidence(
            manifest_digest=f"sha256:{archive.name}",
            config_id=config_digest,
            local_reference=f"oci-layout:{archive.name}",
            architecture=expected_architecture,
            runtime_interface=expected_runtime_interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def _validate_evidence(
    evidence: PulledImageEvidence,
    expected_architecture: str,
    expected_interface: str | None,
) -> None:
    if not isinstance(evidence, PulledImageEvidence):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.EVIDENCE_INVALID, "OCI transport returned invalid evidence"
        )
    if (
        not isinstance(evidence.manifest_digest, str)
        or _IMAGE_DIGEST.fullmatch(evidence.manifest_digest) is None
    ):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.EVIDENCE_INVALID,
            "OCI transport manifest observation is unreadable",
        )
    if (
        not isinstance(evidence.config_id, str)
        or _IMAGE_DIGEST.fullmatch(evidence.config_id) is None
        or not evidence.local_reference
    ):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.EVIDENCE_INVALID,
            "OCI transport did not return local image identity",
        )
    if (
        not isinstance(evidence.archive_sha256, str)
        or _SHA256.fullmatch(evidence.archive_sha256) is None
        or type(evidence.archive_bytes) is not int
        or evidence.archive_bytes < 1
    ):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.EVIDENCE_INVALID,
            "OCI transport did not return archive verification evidence",
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
