"""Controller-owned preparation of exact runtime image archives.

This module is deliberately independent of Run/Switch orchestration.  It gives
the Controller one small seam for turning either a canonical pinned
``runtime-distribution`` document or a succeeded ``RecipeBuild`` receipt into
the same durable, content-verified image receipt.

The transport owns the existing OCI pull/export implementation.  It never
receives a Spark address and this module never uploads to a registry.  The
filesystem adapter is intentionally boring: archives are content addressed,
and receipts are replaced atomically after the archive has been verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RuntimeImagePreparationError(ValueError):
    """A canonical identity, image archive, or receipt is invalid."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class PulledImageEvidence:
    """Evidence returned by the Controller's OCI pull/export implementation."""

    manifest_digest: str
    config_id: str
    local_reference: str
    architecture: str
    runtime_interface: str


class OCIImageTransport(Protocol):
    def pull_and_export(
        self, reference: str, destination: Path
    ) -> PulledImageEvidence:
        """Pull the exact pinned image and export it to ``destination``.

        Implementations may use skopeo, Podman, Docker, or an existing
        Controller image service.  The implementation must return only after
        the export is complete and closed.
        """


@dataclass(frozen=True, slots=True)
class RuntimeImageReceipt:
    """Normalized image identity consumable without a Controller restart."""

    schema_version: int
    source: str
    distribution_publisher: str
    distribution_slug: str
    distribution_content_sha256: str
    registry_manifest_digest: str
    image_digest: str
    oci_archive_sha256: str
    image_bytes: int
    local_image_config_id: str | None
    local_image_reference: str | None
    architecture: str
    runtime_interface: str
    archive_path: str
    recorded_at: str
    build_id: str | None = None

    @property
    def oci_layout_sha256(self) -> str:
        return self.oci_archive_sha256

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


class RuntimeImageStorage(Protocol):
    def prepare_path(self) -> Path:
        """Return a private path for a new export."""

    def commit(
        self,
        staged: Path,
        *,
        receipt: RuntimeImageReceipt,
    ) -> RuntimeImageReceipt:
        """Verify and atomically publish an archive and its receipt."""

    def verify_existing(
        self, archive_sha256: str, expected_bytes: int
    ) -> Path:
        """Return an existing verified archive or raise."""


class FilesystemRuntimeImageStorage:
    """Flat content-addressed archive storage used by Controller/NAS."""

    def __init__(self, root: Path, *, maximum_bytes: int = 16 * 1024**4) -> None:
        self.root = root
        self.maximum_bytes = maximum_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare_path(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f".runtime-image-{uuid.uuid4().hex}.part"

    def commit(
        self, staged: Path, *, receipt: RuntimeImageReceipt
    ) -> RuntimeImageReceipt:
        if not staged.is_file() or staged.is_symlink():
            raise RuntimeImagePreparationError(
                "runtime_image.archive_unavailable", "OCI export did not produce a regular archive"
            )
        size, digest = _file_digest(staged, self.maximum_bytes)
        if size != receipt.image_bytes or digest != receipt.oci_archive_sha256:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch",
                "OCI archive bytes or digest do not match the image receipt",
            )
        final = self.root / receipt.oci_archive_sha256
        if final.exists():
            existing_size, existing_digest = _file_digest(final, self.maximum_bytes)
            if (existing_size, existing_digest) != (size, digest):
                raise RuntimeImagePreparationError(
                    "runtime_image.archive_conflict", "content-addressed OCI archive conflicts"
                )
            if staged != final:
                staged.unlink()
        else:
            os.replace(staged, final)
        published = RuntimeImageReceipt(
            **{
                **receipt.to_mapping(),
                "archive_path": str(final),
            }
        )
        receipt_path = self.root / f"{receipt.oci_archive_sha256}.receipt.json"
        _atomic_json_replace(receipt_path, published.to_mapping())
        return published

    def verify_existing(self, archive_sha256: str, expected_bytes: int) -> Path:
        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_invalid", "OCI archive digest is invalid"
            )
        path = self.root / archive_sha256
        if not path.is_file() or path.is_symlink():
            raise RuntimeImagePreparationError(
                "runtime_image.archive_unavailable", "OCI archive is not present in Controller storage"
            )
        size, digest = _file_digest(path, self.maximum_bytes)
        if size != expected_bytes or digest != archive_sha256:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch", "stored OCI archive failed content verification"
            )
        return path

    def read_receipt(self, archive_sha256: str) -> RuntimeImageReceipt:
        path = self.root / f"{archive_sha256}.receipt.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise TypeError
            return RuntimeImageReceipt(**dict(value))
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_unavailable", "runtime image receipt is unavailable or malformed"
            ) from error


def prepare_runtime_image(
    distribution: Mapping[str, object] | object,
    *,
    storage: RuntimeImageStorage,
    transport: OCIImageTransport | None = None,
    build_receipt: Mapping[str, object] | object | None = None,
    recipe: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> RuntimeImageReceipt:
    """Prepare a pinned image or normalize a successful source-build receipt.

    ``distribution`` is the immutable resolved catalog entity.  It may be the
    entity document itself, or a mapping containing ``document`` and
    ``content_sha256``.  A prebuilt document requires ``transport``; a source
    build requires a succeeded ``build_receipt``.  ``recipe`` may provide the
    canonical runtime interface when it is not repeated on the distribution
    entity.  Both paths verify the archive and publish exactly the same receipt
    shape.
    """

    document, identity = _distribution_document(distribution)
    publisher, slug, content_sha256 = _distribution_identity(document, identity)
    expected_reference, expected_manifest = _pinned_image(document)
    architecture = _string(document.get("platform"), "runtime_image.platform")
    declared_interface = document.get("runtime_interface")
    if declared_interface is None and recipe is not None:
        runtime = recipe.get("runtime")
        if isinstance(runtime, Mapping):
            declared_interface = runtime.get("interface")
    if declared_interface is None and recipe is not None:
        declared_interface = recipe.get("runtime_interface")
    if declared_interface is not None and not isinstance(declared_interface, str):
        raise RuntimeImagePreparationError(
            "runtime_image.interface_invalid", "runtime interface must be a string"
        )
    source_build = build_receipt is not None
    if source_build:
        receipt = _prepare_from_build(
            build_receipt,
            storage=storage,
            publisher=publisher,
            slug=slug,
            content_sha256=content_sha256,
            expected_manifest=expected_manifest,
            architecture=architecture,
            declared_interface=declared_interface,
            now=now,
        )
    else:
        if transport is None:
            raise RuntimeImagePreparationError(
                "runtime_image.transport_required", "prebuilt runtime image requires an OCI transport"
            )
        receipt = _prepare_from_registry(
            transport,
            storage=storage,
            reference=expected_reference,
            expected_manifest=expected_manifest,
            publisher=publisher,
            slug=slug,
            content_sha256=content_sha256,
            architecture=architecture,
            declared_interface=declared_interface,
            now=now,
        )
    if source_build:
        # A source build is allowed to produce a new final image digest.  The
        # pinned distribution digest remains separately preserved as the
        # registry input identity on the receipt.
        if receipt.registry_manifest_digest != expected_manifest:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_mismatch", "source-build receipt lost its pinned registry identity"
            )
    elif receipt.image_digest != expected_manifest:
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch", "prepared image digest does not match the immutable distribution"
        )
    return receipt


def _prepare_from_registry(
    transport: OCIImageTransport,
    *,
    storage: RuntimeImageStorage,
    reference: str,
    expected_manifest: str,
    publisher: str,
    slug: str,
    content_sha256: str,
    architecture: str,
    declared_interface: str | None,
    now: datetime | None,
) -> RuntimeImageReceipt:
    staged = storage.prepare_path()
    try:
        evidence = transport.pull_and_export(reference, staged)
        _validate_evidence(evidence, expected_manifest, architecture, declared_interface)
        image_bytes, archive_sha = _file_digest(staged, 16 * 1024**4)
        interface = evidence.runtime_interface
        receipt = RuntimeImageReceipt(
            schema_version=1,
            source="published",
            distribution_publisher=publisher,
            distribution_slug=slug,
            distribution_content_sha256=content_sha256,
            registry_manifest_digest=evidence.manifest_digest,
            image_digest=evidence.manifest_digest,
            oci_archive_sha256=archive_sha,
            image_bytes=image_bytes,
            local_image_config_id=evidence.config_id,
            local_image_reference=evidence.local_reference,
            architecture=evidence.architecture,
            runtime_interface=interface,
            archive_path=str(staged),
            recorded_at=_timestamp(now),
        )
        return storage.commit(staged, receipt=receipt)
    except RuntimeImagePreparationError:
        _unlink_quietly(staged)
        raise
    except Exception as error:
        _unlink_quietly(staged)
        raise RuntimeImagePreparationError(
            "runtime_image.transport_failed", "OCI pull/export failed"
        ) from error


def _prepare_from_build(
    raw: Mapping[str, object] | object,
    *,
    storage: RuntimeImageStorage,
    publisher: str,
    slug: str,
    content_sha256: str,
    expected_manifest: str,
    architecture: str,
    declared_interface: str | None,
    now: datetime | None,
) -> RuntimeImageReceipt:
    value = _object_mapping(raw)
    if value.get("state") not in {None, "succeeded"}:
        raise RuntimeImagePreparationError(
            "runtime_image.build_incomplete", "source-build receipt is not succeeded"
        )
    image_digest = _string(value.get("image_digest"), "runtime_image.build_digest")
    archive_sha = _string(value.get("oci_layout_sha256"), "runtime_image.build_archive_digest")
    if _IMAGE_DIGEST.fullmatch(image_digest) is None or _SHA256.fullmatch(archive_sha) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid", "source-build image evidence is invalid"
        )
    image_bytes = value.get("image_bytes")
    if type(image_bytes) is not int or image_bytes < 1:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid", "source-build image bytes are invalid"
        )
    storage.verify_existing(archive_sha, image_bytes)
    source_arch = value.get("architecture", architecture)
    if not isinstance(source_arch, str) or source_arch != architecture:
        raise RuntimeImagePreparationError(
            "runtime_image.architecture_mismatch", "source-build architecture does not match the recipe"
        )
    interface = value.get("runtime_interface", declared_interface)
    if not isinstance(interface, str) or not interface:
        raise RuntimeImagePreparationError(
            "runtime_image.interface_missing", "source-build receipt lacks a runtime interface"
        )
    if declared_interface is not None and interface != declared_interface:
        raise RuntimeImagePreparationError(
            "runtime_image.interface_mismatch", "source-build interface does not match the recipe"
        )
    receipt = RuntimeImageReceipt(
        schema_version=1,
        source="controller-build",
        distribution_publisher=publisher,
        distribution_slug=slug,
        distribution_content_sha256=content_sha256,
        registry_manifest_digest=expected_manifest,
        image_digest=image_digest,
        oci_archive_sha256=archive_sha,
        image_bytes=image_bytes,
        local_image_config_id=_optional_string(value.get("config_id"), None),
        local_image_reference=_optional_string(value.get("local_reference"), None),
        architecture=source_arch,
        runtime_interface=interface,
        archive_path=str(getattr(storage, "root", Path("")) / archive_sha),
        recorded_at=_timestamp(now),
        build_id=_optional_string(value.get("build_id"), None),
    )
    # A build receipt already points at an immutable stored archive, but still
    # update the receipt atomically so direct and source-build paths converge.
    existing = storage.verify_existing(archive_sha, image_bytes)
    return storage.commit(existing, receipt=receipt)


def _distribution_document(
    value: Mapping[str, object] | object,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if isinstance(value, Mapping):
        document = value.get("document")
        if isinstance(document, Mapping):
            return document, value
        return value, value
    document = getattr(value, "document", None)
    if not isinstance(document, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.identity_invalid", "resolved distribution document is unavailable"
        )
    return document, {
        "content_sha256": getattr(value, "content_sha256", None),
        "identity": document.get("identity"),
    }


def _distribution_identity(
    document: Mapping[str, object], identity: Mapping[str, object]
) -> tuple[str, str, str]:
    raw = document.get("identity")
    if not isinstance(raw, Mapping):
        raw = identity.get("identity") if isinstance(identity.get("identity"), Mapping) else identity
    publisher = _string(raw.get("publisher"), "runtime_image.publisher")
    slug = _string(raw.get("slug"), "runtime_image.slug")
    content = identity.get("content_sha256", document.get("content_sha256"))
    if _SHA256.fullmatch(str(content)) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.identity_invalid", "immutable distribution content digest is invalid"
        )
    return publisher, slug, str(content)


def _pinned_image(document: Mapping[str, object]) -> tuple[str, str]:
    reference = document.get("image")
    if not isinstance(reference, str) or "@sha256:" not in reference:
        raise RuntimeImagePreparationError(
            "runtime_image.image_unpinned", "runtime distribution image must be pinned by digest"
        )
    expected = "sha256:" + reference.rsplit("@sha256:", 1)[1]
    manifest = document.get("image_manifest")
    manifest_digest = manifest.get("digest") if isinstance(manifest, Mapping) else None
    if _IMAGE_DIGEST.fullmatch(expected) is None or not isinstance(manifest_digest, str):
        raise RuntimeImagePreparationError(
            "runtime_image.manifest_invalid", "runtime distribution image manifest is invalid"
        )
    manifest_full = manifest_digest if manifest_digest.startswith("sha256:") else f"sha256:{manifest_digest}"
    if manifest_full != expected:
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch", "image and image_manifest digests differ"
        )
    return reference, expected


def _validate_evidence(
    evidence: PulledImageEvidence,
    expected_manifest: str,
    expected_architecture: str,
    declared_interface: str | None,
) -> None:
    if not isinstance(evidence, PulledImageEvidence):
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid", "OCI transport returned invalid evidence"
        )
    if evidence.manifest_digest != expected_manifest or _IMAGE_DIGEST.fullmatch(evidence.manifest_digest) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch", "OCI transport returned a different manifest digest"
        )
    if not evidence.config_id or not evidence.local_reference:
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid", "OCI transport did not return local image identity"
        )
    if evidence.architecture != expected_architecture:
        raise RuntimeImagePreparationError(
            "runtime_image.architecture_mismatch", "verified image architecture does not match the recipe"
        )
    if not evidence.runtime_interface or (
        declared_interface is not None and evidence.runtime_interface != declared_interface
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.interface_mismatch", "verified image runtime interface does not match the recipe"
        )


def _object_mapping(value: Mapping[str, object] | object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    data = {
        name: getattr(value, name)
        for name in (
            "state", "image_digest", "oci_layout_sha256", "image_bytes", "build_id",
            "architecture", "runtime_interface", "config_id", "local_reference",
        )
        if hasattr(value, name)
    }
    if not data:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid", "source-build receipt is not readable"
        )
    return data


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeImagePreparationError("runtime_image.identity_invalid", f"{field} is invalid")
    return value


def _optional_string(value: object, fallback: str | None) -> str | None:
    return value if isinstance(value, str) and value else fallback


def _timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _file_digest(path: Path, maximum_bytes: int) -> tuple[int, str]:
    try:
        size = path.stat().st_size
        if size < 1 or size > maximum_bytes:
            raise ValueError("archive size is outside the allowed range")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return size, digest.hexdigest()
    except (OSError, ValueError) as error:
        raise RuntimeImagePreparationError(
            "runtime_image.archive_unavailable", "OCI archive could not be verified"
        ) from error


def _atomic_json_replace(path: Path, value: Mapping[str, object]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        _unlink_quietly(temporary)
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_write_failed", "runtime image receipt could not be recorded"
        ) from error


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "FilesystemRuntimeImageStorage",
    "OCIImageTransport",
    "PulledImageEvidence",
    "RuntimeImagePreparationError",
    "RuntimeImageReceipt",
    "RuntimeImageStorage",
    "prepare_runtime_image",
]
