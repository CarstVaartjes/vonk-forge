"""Controller-owned preparation of exact runtime image archives.

This module is deliberately independent of Run/Switch orchestration.  It gives
the Controller one small seam for turning a canonical pinned ``RecipeDefinition``
or a succeeded ``RecipeBuild`` receipt into the same durable, content-verified
image receipt.

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
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_forge_contracts import RecipeDefinition

from .models import RuntimeImageReceipt as RuntimeImageReceiptRow

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
    requested_manifest_digest: str | None
    config_id: str
    local_reference: str
    architecture: str
    runtime_interface: str
    archive_sha256: str
    archive_bytes: int


class OCIImageTransport(Protocol):
    def pull_and_export(
        self, reference: str, destination: Path, *, expected_architecture: str,
        expected_runtime_interface: str,
    ) -> PulledImageEvidence:
        """Pull the exact pinned image and export it to ``destination``.

        The implementation must return only after the export is complete and
        closed, and must hash the archive while it is copied or exactly once
        after the copy has completed.
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
        """Inspect an already stored final image without pulling a registry image."""


class SkopeoOCIImageTransport:
    """Concrete unprivileged OCI transport backed by packaged ``skopeo``."""

    def __init__(self, *, executable: str = "/usr/bin/skopeo") -> None:
        self.executable = executable

    def pull_and_export(
        self,
        reference: str,
        destination: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> PulledImageEvidence:
        # Recipe/runtime projections carry the Controller wire contract
        # (``vonk.runtime.v1``), while the OCI label stores its short value
        # (``v1``).  Keep that translation at the OCI boundary so callers
        # cannot accidentally compare unlike identities.
        expected_runtime_interface = _runtime_interface_label(expected_runtime_interface)
        source = f"docker://{reference}"
        expected_manifest = _reference_digest(reference)
        observed_digest = _run_text(
            [self.executable, "inspect", *_platform_args(expected_architecture), "--format", "{{.Digest}}", source]
        ).strip()
        raw_manifest = _run_text(
            [self.executable, "inspect", *_platform_args(expected_architecture), "--raw", source]
        )
        config = _run_json_text(
            _run_text(
                [self.executable, "inspect", *_platform_args(expected_architecture), "--config", source]
            )
        )
        if _IMAGE_DIGEST.fullmatch(observed_digest) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_mismatch", "skopeo resolved an invalid platform image digest"
            )
        if not isinstance(config, Mapping):
            raise RuntimeImagePreparationError(
                "runtime_image.inspect_invalid", "skopeo config output is invalid"
            )
        architecture = _observed_architecture(config)
        if architecture != expected_architecture:
            raise RuntimeImagePreparationError(
                "runtime_image.architecture_mismatch", "OCI image architecture does not match the recipe"
            )
        interface = _observed_runtime_interface(config)
        if interface != expected_runtime_interface:
            raise RuntimeImagePreparationError(
                "runtime_image.interface_mismatch", "OCI image runtime interface label does not match the recipe"
            )
        config_id = _config_digest(raw_manifest)
        _run_text(
            [self.executable, "copy", *_platform_args(expected_architecture), source, f"oci-archive:{destination}"]
        )
        archive_bytes, archive_sha = _file_digest(destination, 16 * 1024**4)
        return PulledImageEvidence(
            manifest_digest=observed_digest,
            requested_manifest_digest=expected_manifest,
            config_id=config_id,
            local_reference=reference,
            architecture=architecture,
            runtime_interface=interface,
            archive_sha256=archive_sha,
            archive_bytes=archive_bytes,
        )

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        expected_runtime_interface = _runtime_interface_label(expected_runtime_interface)
        source = f"oci-archive:{archive}"
        observed_digest = _run_text(
            [self.executable, "inspect", *_platform_args(expected_architecture), "--format", "{{.Digest}}", source]
        ).strip()
        raw_manifest = _run_text(
            [self.executable, "inspect", *_platform_args(expected_architecture), "--raw", source]
        )
        config = _run_json_text(
            _run_text(
                [self.executable, "inspect", *_platform_args(expected_architecture), "--config", source]
            )
        )
        if not isinstance(config, Mapping):
            raise RuntimeImagePreparationError(
                "runtime_image.inspect_invalid", "skopeo config output is invalid"
            )
        architecture = _observed_architecture(config)
        if architecture != expected_architecture:
            raise RuntimeImagePreparationError(
                "runtime_image.architecture_mismatch", "OCI image architecture does not match the recipe"
            )
        interface = _observed_runtime_interface(config)
        if interface != expected_runtime_interface:
            raise RuntimeImagePreparationError(
                "runtime_image.interface_mismatch", "OCI image runtime interface label does not match the recipe"
            )
        if _IMAGE_DIGEST.fullmatch(observed_digest) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_mismatch", "skopeo archive inspection returned an invalid image digest"
            )
        return PulledImageEvidence(
            manifest_digest=observed_digest,
            requested_manifest_digest=None,
            config_id=_config_digest(raw_manifest),
            local_reference=source,
            architecture=architecture,
            runtime_interface=interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


@dataclass(frozen=True, slots=True)
class RuntimeImageReceipt:
    """Normalized image identity consumable without a Controller restart."""

    schema_version: int
    source: str
    distribution_publisher: str
    distribution_slug: str
    distribution_content_sha256: str
    registry_manifest_digest: str | None
    platform_manifest_digest: str
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
    # The wire runtime contract and the OCI label are deliberately separate
    # observations.  Older persisted receipts may omit the label; newly
    # prepared receipts always record it.
    runtime_interface_label: str | None = None

    @property
    def oci_layout_sha256(self) -> str:
        return self.oci_archive_sha256

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


def _parse_runtime_image_receipt(value: object) -> RuntimeImageReceipt:
    if not isinstance(value, Mapping) or value.get("schema_version") != 2:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid",
            "runtime image receipt schema version is unsupported",
        )
    try:
        return RuntimeImageReceipt(**dict(value))
    except (TypeError, ValueError, KeyError) as error:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_unavailable",
            "runtime image receipt is unavailable or malformed",
        ) from error


def persist_runtime_image_receipt(
    session: Session,
    *,
    recipe_revision_id: str,
    original_content_digest: str,
    effective_execution_key: str,
    receipt: RuntimeImageReceipt,
    verified_at: datetime,
) -> RuntimeImageReceiptRow:
    """Atomically upsert the verified runtime identity in Controller SQL.

    Filesystem receipts are an object cache.  The SQL row is the authority
    consumed by install admission and agent specification reads, so a
    successful filesystem preparation is not complete until this function
    flushes the exact recipe/effective execution binding in the caller's
    transaction.
    """

    if (
        not isinstance(recipe_revision_id, str)
        or not isinstance(original_content_digest, str)
        or not isinstance(effective_execution_key, str)
        or len(original_content_digest) != 64
        or len(effective_execution_key) != 64
        or any(
            character not in "0123456789abcdef"
            for value in (original_content_digest, effective_execution_key)
            for character in value
        )
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_identity_invalid",
            "runtime image receipt recipe identity is invalid",
        )
    if receipt.source not in {"published", "controller-build"}:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_identity_invalid",
            "runtime image receipt source is invalid",
        )
    if receipt.distribution_content_sha256 != original_content_digest:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_identity_invalid",
            "runtime image receipt recipe digest does not match the catalog revision",
        )
    if receipt.source == "published":
        if receipt.registry_manifest_digest is None or receipt.build_id is not None:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_identity_invalid",
                "published runtime image receipt provenance is invalid",
            )
    elif receipt.registry_manifest_digest is not None or receipt.build_id is None:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_identity_invalid",
            "Controller-build runtime image receipt provenance is invalid",
        )
    if (
        receipt.architecture != "linux-arm64"
        or receipt.runtime_interface != "vonk.runtime.v1"
        or receipt.runtime_interface_label != "v1"
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_identity_invalid",
            "runtime image platform or interface identity is invalid",
        )
    lookup = select(RuntimeImageReceiptRow).where(
        RuntimeImageReceiptRow.recipe_revision_id == recipe_revision_id,
        RuntimeImageReceiptRow.source == receipt.source,
        RuntimeImageReceiptRow.original_content_digest == original_content_digest,
        RuntimeImageReceiptRow.effective_execution_key == effective_execution_key,
    )
    row = session.scalar(lookup)
    identity = {
        "registry_manifest_digest": receipt.registry_manifest_digest,
        "platform_manifest_digest": receipt.platform_manifest_digest,
        "local_image_config_id": receipt.local_image_config_id,
        "oci_archive_sha256": receipt.oci_archive_sha256,
        "image_bytes": receipt.image_bytes,
        "architecture": receipt.architecture,
        "runtime_interface": receipt.runtime_interface,
        "runtime_interface_label": receipt.runtime_interface_label,
        "build_id": receipt.build_id,
    }
    if row is None:
        row = RuntimeImageReceiptRow(
            recipe_revision_id=recipe_revision_id,
            source=receipt.source,
            original_content_digest=original_content_digest,
            effective_execution_key=effective_execution_key,
            **identity,
            verified_at=verified_at,
            state="verified",
        )
        session.add(row)
    else:
        existing = {
            key: getattr(row, key)
            for key in identity
        }
        if existing != identity:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_identity_conflict",
                "durable runtime image receipt identity changed for the same execution",
            )
        row.verified_at = verified_at
        row.state = "verified"
    session.flush()
    return row


def resolve_persisted_runtime_image_receipt(
    session: Session,
    *,
    recipe_revision_id: str,
    original_content_digest: str,
    effective_execution_key: str,
    receipt: RuntimeImageReceipt,
) -> RuntimeImageReceiptRow:
    """Require SQL to match every verified filesystem identity field."""

    row = session.scalar(
        select(RuntimeImageReceiptRow).where(
            RuntimeImageReceiptRow.recipe_revision_id == recipe_revision_id,
            RuntimeImageReceiptRow.source == receipt.source,
            RuntimeImageReceiptRow.original_content_digest == original_content_digest,
            RuntimeImageReceiptRow.effective_execution_key == effective_execution_key,
            RuntimeImageReceiptRow.state == "verified",
            RuntimeImageReceiptRow.registry_manifest_digest == receipt.registry_manifest_digest,
            RuntimeImageReceiptRow.platform_manifest_digest == receipt.platform_manifest_digest,
            RuntimeImageReceiptRow.local_image_config_id == receipt.local_image_config_id,
            RuntimeImageReceiptRow.oci_archive_sha256 == receipt.oci_archive_sha256,
            RuntimeImageReceiptRow.image_bytes == receipt.image_bytes,
            RuntimeImageReceiptRow.architecture == receipt.architecture,
            RuntimeImageReceiptRow.runtime_interface == receipt.runtime_interface,
            RuntimeImageReceiptRow.runtime_interface_label == receipt.runtime_interface_label,
            RuntimeImageReceiptRow.build_id == receipt.build_id,
        )
    )
    if row is None:
        raise ValueError("durable runtime image receipt identity does not match filesystem receipt")
    return row


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

    def find_published(
        self,
        registry_manifest_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceipt | None:
        """Find and verify a previously published image by immutable identity."""

    def find_verified(
        self,
        image_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceipt | None:
        """Find and verify a prepared image without invoking a transport."""


class FilesystemRuntimeImageStorage:
    """Content-addressed Controller/NAS storage under the OCI namespace.

    The caller supplies the shared Controller artifact root.  Runtime image
    archives and their receipts are kept in the explicit ``oci-archives``
    namespace so model/build objects cannot be confused with OCI payloads.
    """

    def __init__(self, root: Path, *, maximum_bytes: int = 16 * 1024**4) -> None:
        self.root = root / "oci-archives"
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
        size = staged.stat().st_size
        if (
            size < 1
            or size > self.maximum_bytes
            or size != receipt.image_bytes
            or not _SHA256.fullmatch(receipt.oci_archive_sha256)
        ):
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch",
                "OCI archive bytes or digest do not match the image receipt",
            )
        final = self.root / receipt.oci_archive_sha256
        if final.exists():
            existing_size = final.stat().st_size
            if existing_size != size:
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

    def find_published(
        self,
        registry_manifest_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceipt | None:
        """Read the atomic receipt index before starting another OCI export.

        Receipt files are the content-addressed index: the archive is still
        re-hashed before reuse, so a partial or corrupt object fails loudly
        and cannot be mistaken for a cache hit.
        """

        expected_architecture = _wire_architecture(expected_architecture)
        expected_interface = expected_runtime_interface
        expected_label = _runtime_interface_label(expected_interface)
        for receipt_path in sorted(self.root.glob("*.receipt.json")):
            try:
                value = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt = _parse_runtime_image_receipt(value)
            except RuntimeImagePreparationError:
                raise
            except (OSError, TypeError, ValueError, KeyError) as error:
                raise RuntimeImagePreparationError(
                    "runtime_image.receipt_unavailable",
                    "runtime image receipt index is malformed",
                ) from error
            if (
                receipt.source != "published"
                or receipt.registry_manifest_digest != registry_manifest_digest
                or receipt.architecture != expected_architecture
                or receipt.runtime_interface != expected_interface
                or receipt.runtime_interface_label not in {None, expected_label}
            ):
                continue
            if (
                _IMAGE_DIGEST.fullmatch(receipt.registry_manifest_digest or "") is None
                or _IMAGE_DIGEST.fullmatch(receipt.platform_manifest_digest) is None
                or receipt.platform_manifest_digest != receipt.image_digest
                or _IMAGE_DIGEST.fullmatch(receipt.local_image_config_id or "") is None
            ):
                raise RuntimeImagePreparationError(
                    "runtime_image.receipt_invalid",
                    "published runtime image receipt identity is malformed",
                )
            self.verify_existing(receipt.oci_archive_sha256, receipt.image_bytes)
            return receipt
        return None

    def find_verified(
        self,
        image_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceipt | None:
        """Resolve a verified receipt without invoking an image transport.

        Preview, admission, and agent spec reads use this read-only side of
        the image boundary.  The durable worker is responsible for calling
        :func:`prepare_runtime_image` when the receipt is absent.  Published
        parent manifests and Controller-built platform images are both valid
        lookup identities, while the archive is re-hashed before reuse.
        """

        if _IMAGE_DIGEST.fullmatch(image_digest) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_invalid", "runtime image identity is invalid"
            )
        expected_architecture = _wire_architecture(expected_architecture)
        expected_label = _runtime_interface_label(expected_runtime_interface)
        for receipt_path in sorted(self.root.glob("*.receipt.json")):
            try:
                value = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt = _parse_runtime_image_receipt(value)
            except RuntimeImagePreparationError:
                raise
            except (OSError, TypeError, ValueError, KeyError) as error:
                raise RuntimeImagePreparationError(
                    "runtime_image.receipt_unavailable",
                    "runtime image receipt index is malformed",
                ) from error
            if receipt.source == "published":
                matches = receipt.registry_manifest_digest == image_digest
            elif receipt.source == "controller-build":
                matches = receipt.image_digest == image_digest
            else:
                matches = False
            if not matches:
                continue
            if (
                receipt.architecture != expected_architecture
                or receipt.runtime_interface != expected_runtime_interface
                or receipt.runtime_interface_label not in {None, expected_label}
                or _IMAGE_DIGEST.fullmatch(receipt.platform_manifest_digest) is None
                or receipt.platform_manifest_digest != receipt.image_digest
                or _IMAGE_DIGEST.fullmatch(receipt.local_image_config_id or "") is None
            ):
                raise RuntimeImagePreparationError(
                    "runtime_image.receipt_invalid",
                    "runtime image receipt does not prove the requested platform identity",
                )
            self.verify_existing(receipt.oci_archive_sha256, receipt.image_bytes)
            return receipt
        return None

    def read_receipt(self, archive_sha256: str) -> RuntimeImageReceipt:
        path = self.root / f"{archive_sha256}.receipt.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return _parse_runtime_image_receipt(value)
        except RuntimeImagePreparationError:
            raise
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_unavailable", "runtime image receipt is unavailable or malformed"
            ) from error


def prepare_runtime_image(
    recipe: RecipeDefinition | Mapping[str, object] | object,
    *,
    runtime: Mapping[str, object] | object,
    storage: RuntimeImageStorage,
    transport: OCIImageTransport | None = None,
    build_receipt: Mapping[str, object] | object | None = None,
    now: datetime | None = None,
    receipt_writer: Callable[[RuntimeImageReceipt], object] | None = None,
) -> RuntimeImageReceipt:
    """Prepare the image selected by a canonical ``RecipeDefinition``.

    The recipe's execution image or successful build receipt is authoritative;
    ``runtime`` is the already compiled runtime projection and supplies the
    expected architecture/interface.  A catalog distribution document is
    deliberately not accepted as an authority here.
    """

    parsed = _canonical_recipe(recipe)
    projection = _runtime_projection(runtime)
    source_build = parsed.execution.mode == "build"
    if source_build != (build_receipt is not None):
        raise RuntimeImagePreparationError(
            "runtime_image.source_mismatch", "recipe execution mode and build receipt disagree"
        )
    effective_transport = transport or SkopeoOCIImageTransport()
    if source_build:
        receipt = _prepare_from_build(
            build_receipt,
            storage=storage,
            transport=effective_transport,
            publisher=parsed.identity.publisher,
            slug=parsed.identity.slug,
            content_sha256=_recipe_digest(parsed),
            expected_architecture=projection["architecture"],
            expected_interface=projection["interface"],
            now=now,
        )
    else:
        expected_reference, expected_manifest = _recipe_image(parsed)
        receipt = _prepare_from_registry(
            effective_transport,
            storage=storage,
            reference=expected_reference,
            expected_manifest=expected_manifest,
            publisher=parsed.identity.publisher,
            slug=parsed.identity.slug,
            content_sha256=_recipe_digest(parsed),
            expected_architecture=projection["architecture"],
            expected_interface=projection["interface"],
            now=now,
        )
    if not source_build and receipt.registry_manifest_digest != expected_manifest:
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch", "prepared image digest does not match the immutable distribution"
        )
    if receipt_writer is not None:
        try:
            receipt_writer(receipt)
        except RuntimeImagePreparationError:
            raise
        except Exception as error:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_persistence_failed",
                "durable runtime image receipt could not be persisted",
            ) from error
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
    expected_architecture: str,
    expected_interface: str,
    now: datetime | None,
) -> RuntimeImageReceipt:
    cached = storage.find_published(
        expected_manifest,
        expected_architecture=expected_architecture,
        expected_runtime_interface=expected_interface,
    )
    if cached is not None:
        return cached
    staged = storage.prepare_path()
    expected_interface_label = _runtime_interface_label(expected_interface)
    try:
        evidence = transport.pull_and_export(
            reference,
            staged,
            expected_architecture=expected_architecture,
            expected_runtime_interface=expected_interface_label,
        )
        _validate_evidence(
            evidence,
            expected_manifest,
            expected_architecture,
            expected_interface_label,
            expected_requested_manifest=expected_manifest,
        )
        image_bytes, archive_sha = evidence.archive_bytes, evidence.archive_sha256
        receipt = RuntimeImageReceipt(
            schema_version=2,
            source="published",
            distribution_publisher=publisher,
            distribution_slug=slug,
            distribution_content_sha256=content_sha256,
            registry_manifest_digest=expected_manifest,
            platform_manifest_digest=evidence.manifest_digest,
            image_digest=evidence.manifest_digest,
            oci_archive_sha256=archive_sha,
            image_bytes=image_bytes,
            local_image_config_id=evidence.config_id,
            # The OCI transport reference is a registry/archive source.  It
            # is not a post-import Spark start reference; the helper derives
            # that only after inspecting the imported config digest.
            local_image_reference=None,
            architecture=_wire_architecture(evidence.architecture),
            runtime_interface=expected_interface,
            runtime_interface_label=evidence.runtime_interface,
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
    transport: OCIImageTransport,
    publisher: str,
    slug: str,
    content_sha256: str,
    expected_architecture: str,
    expected_interface: str,
    now: datetime | None,
) -> RuntimeImageReceipt:
    value = _object_mapping(raw)
    if value.get("state") != "succeeded":
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
    existing = storage.verify_existing(archive_sha, image_bytes)
    expected_interface_label = _runtime_interface_label(expected_interface)
    observed = transport.inspect_archive(
        existing,
        expected_architecture=expected_architecture,
        expected_runtime_interface=expected_interface_label,
        expected_archive_sha256=archive_sha,
        expected_archive_bytes=image_bytes,
    )
    _validate_evidence(
        observed,
        image_digest,
        expected_architecture,
        expected_interface_label,
        expected_requested_manifest=None,
    )
    receipt = RuntimeImageReceipt(
        schema_version=2,
        source="controller-build",
        distribution_publisher=publisher,
        distribution_slug=slug,
        distribution_content_sha256=content_sha256,
        registry_manifest_digest=_optional_string(value.get("registry_manifest_digest"), None),
        platform_manifest_digest=observed.manifest_digest,
        image_digest=image_digest,
        oci_archive_sha256=archive_sha,
        image_bytes=image_bytes,
        local_image_config_id=observed.config_id,
        # ``oci-archive:...`` is a Controller transport path, never a
        # runnable Spark image reference.
        local_image_reference=None,
        architecture=_wire_architecture(observed.architecture),
        runtime_interface=expected_interface,
        runtime_interface_label=observed.runtime_interface,
        archive_path=str(getattr(storage, "root", Path("")) / archive_sha),
        recorded_at=_timestamp(now),
        build_id=_optional_string(value.get("build_id"), None),
    )
    # A build receipt already points at an immutable stored archive, but still
    # update the receipt atomically so direct and source-build paths converge.
    return storage.commit(existing, receipt=receipt)


def _canonical_recipe(value: RecipeDefinition | Mapping[str, object] | object) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    raw = getattr(value, "document", value)
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.recipe_invalid", "canonical RecipeDefinition document is unavailable"
        )
    try:
        return RecipeDefinition.model_validate(raw)
    except Exception as error:
        raise RuntimeImagePreparationError(
            "runtime_image.recipe_invalid", "recipe does not satisfy canonical RecipeDefinition"
        ) from error


def _runtime_projection(value: Mapping[str, object] | object) -> dict[str, str]:
    raw = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid", "canonical runtime projection is unavailable"
        )
    architecture = raw.get("architecture")
    interface = raw.get("interface", raw.get("runtime_interface"))
    if not isinstance(architecture, str) or not architecture or not isinstance(interface, str) or not interface:
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid", "runtime projection lacks observed architecture/interface expectations"
        )
    return {"architecture": architecture, "interface": interface}


def _runtime_interface_label(value: str) -> str:
    """Map the Controller runtime contract to the OCI label value.

    ``vonk.runtime.v1`` identifies the wire/runtime contract while images
    carry the compact ``v1`` label.  Keeping the two values separate avoids
    treating an inspected label as the launch protocol identity.
    """

    prefix = "vonk.runtime."
    return value.removeprefix(prefix) if value.startswith(prefix) else value


def _wire_architecture(value: str) -> str:
    if value in {"linux/arm64", "linux-aarch64", "arm64", "aarch64"}:
        return "linux-arm64"
    return value


def _recipe_image(recipe: RecipeDefinition) -> tuple[str, str]:
    if recipe.execution.mode != "image":
        raise RuntimeImagePreparationError("runtime_image.source_mismatch", "recipe does not select a direct image")
    image = recipe.execution.image
    digest = f"sha256:{image.digest}"
    return f"{image.repository}@{digest}", digest


def _recipe_digest(recipe: RecipeDefinition) -> str:
    from vonk_forge_contracts import content_sha256

    return content_sha256(recipe)


def _validate_evidence(
    evidence: PulledImageEvidence,
    expected_manifest: str,
    expected_architecture: str,
    expected_interface: str | None,
    *,
    expected_requested_manifest: str | None = None,
) -> None:
    if not isinstance(evidence, PulledImageEvidence):
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid", "OCI transport returned invalid evidence"
        )
    if (
        expected_requested_manifest is not None
        and evidence.requested_manifest_digest != expected_requested_manifest
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch", "OCI transport used a different recipe image digest"
        )
    if _IMAGE_DIGEST.fullmatch(evidence.manifest_digest) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch", "OCI transport returned a different manifest digest"
        )
    if _IMAGE_DIGEST.fullmatch(evidence.config_id) is None or not evidence.local_reference:
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid", "OCI transport did not return local image identity"
        )
    if (
        _SHA256.fullmatch(evidence.archive_sha256) is None
        or type(evidence.archive_bytes) is not int
        or evidence.archive_bytes < 1
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid", "OCI transport did not return archive verification evidence"
        )
    if evidence.architecture != expected_architecture:
        raise RuntimeImagePreparationError(
            "runtime_image.architecture_mismatch", "verified image architecture does not match the recipe"
        )
    if not evidence.runtime_interface or (
        expected_interface is not None and evidence.runtime_interface != expected_interface
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.interface_mismatch", "verified image runtime interface does not match the recipe"
        )


def _run_text(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeImagePreparationError(
            "runtime_image.transport_failed", "packaged OCI helper command failed"
        ) from error
    return result.stdout


def _run_json_text(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeImagePreparationError(
            "runtime_image.inspect_invalid", "packaged OCI helper returned invalid JSON"
        ) from error


def _reference_digest(reference: str) -> str:
    digest = reference.rsplit("@", 1)[-1] if "@" in reference else ""
    if _IMAGE_DIGEST.fullmatch(digest) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.image_unpinned", "recipe image is not digest pinned"
        )
    return digest


def _platform_args(architecture: str) -> list[str]:
    try:
        os_name, cpu = architecture.split("/", 1)
    except ValueError as error:
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid", "runtime architecture must be os/architecture"
        ) from error
    if not os_name or not cpu or "/" in cpu:
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid", "runtime architecture must be os/architecture"
        )
    return ["--override-os", os_name, "--override-arch", cpu]


def _observed_architecture(image: Mapping[str, object]) -> str:
    os_name, architecture = image.get("os", image.get("Os")), image.get("architecture", image.get("Architecture"))
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        raise RuntimeImagePreparationError(
            "runtime_image.architecture_missing", "OCI image platform is missing"
        )
    return f"{os_name}/{architecture}"


def _observed_runtime_interface(image: Mapping[str, object]) -> str:
    labels = image.get("config", image.get("Config"))
    if isinstance(labels, Mapping):
        labels = labels.get("Labels", labels.get("labels"))
    if not isinstance(labels, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.interface_missing", "OCI image runtime interface label is missing"
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
        raise RuntimeImagePreparationError(
            "runtime_image.interface_missing", "OCI image runtime interface label is missing or ambiguous"
        )
    return values.pop()


def _config_digest(raw_manifest: str) -> str:
    value = _run_json_text(raw_manifest)
    config = value.get("config") if isinstance(value, Mapping) else None
    digest = config.get("digest") if isinstance(config, Mapping) else None
    if not isinstance(digest, str) or _IMAGE_DIGEST.fullmatch(digest) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.config_missing", "OCI manifest config digest is missing"
        )
    return digest


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
    "SkopeoOCIImageTransport",
    "persist_runtime_image_receipt",
    "prepare_runtime_image",
    "resolve_persisted_runtime_image_receipt",
]
