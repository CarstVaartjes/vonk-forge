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

import fcntl
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol.wire_model import Digest, WireModel
from vonk_forge_contracts import RecipeDefinition

from .cached_file_verification import verified_files
from .catalog_revision_contract import read_catalog_document, read_catalog_projection
from .models import CatalogDocumentRevision, RecipeBuild, RuntimeImageAuthorization
from .models import RuntimeImageReceipt as RuntimeImageReceiptRow

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_CACHE_DIRECTORY = "image-cache"


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
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> PulledImageEvidence:
        """Pull the exact pinned image and export it to ``destination``.

        The implementation must return only after the export is complete and
        closed, and must hash the archive while it is copied or exactly once
        after the copy has completed.
        """
        ...

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
        ...


class SkopeoLayerMetadata(BaseModel):
    digest: str = Field(alias="Digest")
    size: int = Field(alias="Size")


class SkopeoImageMetadata(BaseModel):
    # Provider metadata is extensible; only declared fields drive progress.
    layers: list[SkopeoLayerMetadata] = Field(default_factory=list, alias="LayersData")


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
        progress: Callable[[str, int, int | None], None] | None = None,
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
        config = _run_json_text(
            _run_text(
                [self.executable, "inspect", *_platform_args(expected_architecture), "--config", source]
            )
        )
        if observed_digest != expected_manifest:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_mismatch", "skopeo resolved a different recipe image digest"
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
        # Keep native OCI blobs between attempts and share completed layers
        # across images. Streaming straight into a tar discards this reuse on
        # interruption. Skopeo owns concurrent layers and transient retries;
        # only the final local conversion creates the runnable archive.
        cache = destination.parent / "registry-layers"
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{reference}\n{expected_architecture}".encode()).hexdigest()
        layout = cache / key
        blobs = cache / "blobs"
        blobs.mkdir(exist_ok=True)
        staged_source = f"oci:{layout}:image"
        # Different images can transfer concurrently. Only writers of the
        # same OCI index are serialized, including across worker processes.
        with (cache / f"{key}.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Skopeo cleans failed writes itself. Remove leftovers after a
            # killed worker once this image's exclusive lock proves no writer
            # can still be using them; completed shared blobs remain reusable.
            for abandoned in layout.glob("oci-put-blob*"):
                abandoned.unlink(missing_ok=True)
            metadata = SkopeoImageMetadata.model_validate_json(_run_text([
                self.executable, "inspect", *_platform_args(expected_architecture), source,
            ]))
            layer_paths = [blobs / value.digest.replace(":", "/", 1)
                           for value in metadata.layers
                           if _IMAGE_DIGEST.fullmatch(value.digest)]
            total = (sum(value.size for value in metadata.layers)
                     if metadata.layers and all(value.size >= 0 for value in metadata.layers)
                     else None)
            _run_with_progress([
                self.executable, "copy", *_platform_args(expected_architecture),
                "--retry-times", "3", "--image-parallel-copies", "6",
                "--dest-oci-accept-uncompressed-layers",
                "--dest-shared-blob-dir", str(blobs), source, staged_source,
            ], lambda: _existing_bytes(layer_paths) + _existing_bytes(layout.glob("oci-put-blob*")),
                progress, "download", total)
            _run_with_progress([
                self.executable, "copy", *_platform_args(expected_architecture),
                "--src-shared-blob-dir", str(blobs), staged_source,
                f"docker-archive:{destination}",
            ], lambda: _existing_bytes([destination]), progress, "prepare", None)
        archive_bytes, archive_sha = _file_digest(destination, 16 * 1024**4)
        # Registry pins may identify a multi-platform index. Docker's native
        # archive conversion also changes manifest representation. Inspect the
        # actual exported single-platform image instead of treating the source
        # index digest as its runnable identity.
        exported = self.inspect_archive(
            destination,
            expected_architecture=expected_architecture,
            expected_runtime_interface=expected_runtime_interface,
            expected_archive_sha256=archive_sha,
            expected_archive_bytes=archive_bytes,
        )
        return replace(
            exported,
            requested_manifest_digest=expected_manifest,
            local_reference=reference,
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
        source = f"docker-archive:{archive}"
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


ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

# The runtime image receipt declares one supported platform identity.  These
# named aliases are the single definition used by both the receipt fields and
# the transport-evidence narrowing below, so the two cannot drift.
RuntimeArchitecture = Literal["linux-arm64"]
RuntimeInterface = Literal["vonk.runtime.v1"]
RuntimeInterfaceLabel = Literal["v1"]
_RUNTIME_ARCHITECTURE: RuntimeArchitecture = "linux-arm64"
_RUNTIME_INTERFACE: RuntimeInterface = "vonk.runtime.v1"
_RUNTIME_INTERFACE_LABEL: RuntimeInterfaceLabel = "v1"


class RuntimeImageReceipt(WireModel):
    """Strict schema-2 receipt persisted by the Controller image cache.

    ``oci_archive_sha256`` is the established filesystem/SQL receipt field.
    The compiled launch plan uses its own ``oci_layout_sha256`` field; the
    execution-plan service performs that one explicit typed projection at the
    plan boundary.
    """

    schema_version: Literal[2]
    source: Literal["published", "controller-build"]
    distribution_publisher: str = Field(min_length=1, max_length=128)
    distribution_slug: str = Field(min_length=1, max_length=128)
    distribution_content_sha256: Digest
    registry_manifest_digest: ImageDigest | None
    platform_manifest_digest: ImageDigest
    image_digest: ImageDigest
    oci_archive_sha256: Digest
    image_bytes: int = Field(strict=True, ge=1, le=16 * 1024**4)
    local_image_config_id: ImageDigest | None
    local_image_reference: str | None = Field(min_length=1, max_length=512)
    architecture: RuntimeArchitecture
    runtime_interface: RuntimeInterface
    archive_path: str = Field(min_length=1, max_length=4096)
    recorded_at: str = Field(min_length=1, max_length=128)
    build_id: str | None = Field(min_length=1, max_length=128)
    runtime_interface_label: RuntimeInterfaceLabel

    @model_validator(mode="after")
    def receipt_identity_is_consistent(self) -> RuntimeImageReceipt:
        if self.platform_manifest_digest != self.image_digest:
            raise ValueError("runtime image receipt platform and image digests differ")
        if self.source == "published" and (
            self.registry_manifest_digest is None or self.build_id is not None
        ):
            raise ValueError("published runtime image receipt provenance is invalid")
        if self.source == "controller-build" and (
            self.registry_manifest_digest is not None or self.build_id is None
        ):
            raise ValueError("Controller-build runtime image receipt provenance is invalid")
        return self

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _parse_runtime_image_receipt(value: object) -> RuntimeImageReceipt:
    if not isinstance(value, Mapping) or value.get("schema_version") != 2:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid",
            "runtime image receipt schema version is unsupported",
        )
    try:
        return RuntimeImageReceipt.model_validate(value)
    except (TypeError, ValueError) as error:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_unavailable",
            "runtime image receipt identity is unavailable or malformed",
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
    current_revision = session.get(CatalogDocumentRevision, recipe_revision_id)
    if (
        current_revision is None
        or current_revision.kind != "recipe"
        or current_revision.state != "active"
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe revision authority is unavailable or inactive",
        )
    if receipt.distribution_content_sha256 != original_content_digest:
        # Production callers provide the currently requested revision digest.
        # Permit that value to differ only when the current SQL revision is a
        # real notes-only successor; the receipt's own digest remains the
        # immutable original provenance used below.
        if current_revision.content_digest != original_content_digest:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_identity_invalid",
                "runtime image receipt recipe digest does not match the catalog revision",
            )
        original_content_digest = receipt.distribution_content_sha256
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
    # The receipt's own content digest selects its original canonical
    # provenance.  ``recipe_revision_id`` is the currently requested revision
    # and may be a notes-only descendant of that original revision.
    original_revision = session.scalar(
        select(CatalogDocumentRevision)
        .where(
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.content_digest == receipt.distribution_content_sha256,
        )
        .order_by(CatalogDocumentRevision.revision_number, CatalogDocumentRevision.id)
        .limit(1)
    )
    if original_revision is None:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_identity_invalid",
            "original recipe revision authority is unavailable",
        )
    original_revision_id = original_revision.id
    _validate_revision_reuse_identity(current_revision, original_revision)
    lookup = select(RuntimeImageReceiptRow).where(
        RuntimeImageReceiptRow.recipe_revision_id == original_revision_id,
        RuntimeImageReceiptRow.source == receipt.source,
        RuntimeImageReceiptRow.original_content_digest == original_content_digest,
        RuntimeImageReceiptRow.effective_execution_key == effective_execution_key,
    )
    conflicting_artifact = session.scalar(
        select(RuntimeImageReceiptRow).where(
            RuntimeImageReceiptRow.recipe_revision_id == original_revision_id,
            RuntimeImageReceiptRow.source == receipt.source,
            RuntimeImageReceiptRow.original_content_digest == original_content_digest,
            RuntimeImageReceiptRow.platform_manifest_digest == receipt.platform_manifest_digest,
            RuntimeImageReceiptRow.local_image_config_id == receipt.local_image_config_id,
            RuntimeImageReceiptRow.oci_archive_sha256 == receipt.oci_archive_sha256,
            RuntimeImageReceiptRow.image_bytes == receipt.image_bytes,
        )
    )
    if (
        conflicting_artifact is not None
        and conflicting_artifact.effective_execution_key != effective_execution_key
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe execution identity does not match the immutable receipt",
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
            recipe_revision_id=original_revision_id,
            source=receipt.source,
            original_content_digest=original_content_digest,
            effective_execution_key=effective_execution_key,
            **identity,
            verified_at=verified_at,
            state="verified",
        )
        session.add(row)
    else:
        if row.state != "verified":
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_authority_revoked",
                "immutable runtime image receipt is not verified",
            )
        existing = {
            key: getattr(row, key)
            for key in identity
        }
        if existing != identity:
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_identity_conflict",
                "durable runtime image receipt identity changed for the same execution",
            )
        # The bytes and original provenance are immutable.  Refreshing the
        # observation timestamp is safe and preserves idempotent preparation.
        row.verified_at = verified_at
        row.state = "verified"
    session.flush()

    _authorize_current_revision(
        session,
        recipe_revision_id=recipe_revision_id,
        receipt=row,
        effective_execution_key=effective_execution_key,
        authorized_at=verified_at,
    )
    return row


def resolve_persisted_runtime_image_receipt(
    session: Session,
    *,
    recipe_revision_id: str,
    current_content_digest: str,
    effective_execution_key: str,
    receipt: RuntimeImageReceipt,
) -> RuntimeImageReceiptRow:
    """Require current-revision authorization for an immutable receipt.

    The filesystem receipt carries the original verified recipe digest.  The
    caller supplies the currently requested revision digest separately so a
    notes-only successor can authorize those same bytes without rewriting the
    original receipt provenance.
    """

    revision = session.get(CatalogDocumentRevision, recipe_revision_id)
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.state != "active"
        or revision.content_digest != current_content_digest
    ):
        raise ValueError("current recipe revision digest is not authorized")
    original_content_digest = receipt.distribution_content_sha256

    row = session.scalar(
        select(RuntimeImageReceiptRow).where(
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
    authorization = session.scalar(
        select(RuntimeImageAuthorization).where(
            RuntimeImageAuthorization.recipe_revision_id == recipe_revision_id,
            RuntimeImageAuthorization.receipt_id == row.id,
            RuntimeImageAuthorization.source == receipt.source,
            RuntimeImageAuthorization.original_content_digest == row.original_content_digest,
            RuntimeImageAuthorization.effective_execution_key == effective_execution_key,
            RuntimeImageAuthorization.registry_manifest_digest == receipt.registry_manifest_digest,
            RuntimeImageAuthorization.platform_manifest_digest == receipt.platform_manifest_digest,
            RuntimeImageAuthorization.local_image_config_id == receipt.local_image_config_id,
            RuntimeImageAuthorization.oci_archive_sha256 == receipt.oci_archive_sha256,
            RuntimeImageAuthorization.image_bytes == receipt.image_bytes,
            RuntimeImageAuthorization.build_id == receipt.build_id,
            RuntimeImageAuthorization.state == "authorized",
        )
    )
    if authorization is None:
        raise ValueError("current recipe revision is not authorized for runtime image receipt")
    return row


def _authorize_current_revision(
    session: Session,
    *,
    recipe_revision_id: str,
    receipt: RuntimeImageReceiptRow,
    effective_execution_key: str,
    authorized_at: datetime,
) -> RuntimeImageAuthorization:
    """Create or verify the separate current-recipe receipt binding."""

    if receipt.effective_execution_key != effective_execution_key:
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe execution identity does not match the immutable receipt",
        )

    revision = session.get(CatalogDocumentRevision, recipe_revision_id)
    if revision is None or revision.kind != "recipe" or revision.state != "active":
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe revision authority is unavailable or inactive",
        )
    try:
        recipe = read_catalog_document(revision)
    except ValueError as error:
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe document is not valid persisted contract JSON",
        ) from error
    execution = recipe.execution if isinstance(recipe, RecipeDefinition) else None
    if execution is not None and execution.mode == "image":
        image = execution.image
        raw_digest = image.digest
        expected = f"sha256:{raw_digest}"
        if receipt.source != "published" or expected != receipt.registry_manifest_digest:
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_invalid",
                "current recipe image authority does not match the verified receipt",
            )
    elif execution is not None and execution.mode == "build":
        if receipt.source != "controller-build" or receipt.build_id is None:
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_invalid",
                "current source-build recipe has no matching build receipt",
            )
        build = session.get(RecipeBuild, receipt.build_id)
        if (
            build is None
            or build.state != "succeeded"
            or build.image_digest != receipt.platform_manifest_digest
            or build.oci_layout_sha256 != receipt.oci_archive_sha256
            or build.image_bytes != receipt.image_bytes
        ):
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_invalid",
                "source-build receipt is not backed by the exact succeeded build",
            )
        source_bundle_sha256 = read_catalog_projection(revision).source_bundle_sha256
        if source_bundle_sha256 is None or build.source_bundle_sha256 != source_bundle_sha256:
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_invalid",
                "source-build receipt does not match the current build input",
            )
    else:
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe execution discriminator is unavailable",
        )

    values = {
        "source": receipt.source,
        "original_content_digest": receipt.original_content_digest,
        "effective_execution_key": effective_execution_key,
        "registry_manifest_digest": receipt.registry_manifest_digest,
        "platform_manifest_digest": receipt.platform_manifest_digest,
        "local_image_config_id": receipt.local_image_config_id,
        "oci_archive_sha256": receipt.oci_archive_sha256,
        "image_bytes": receipt.image_bytes,
        "build_id": receipt.build_id,
    }
    authorization = session.scalar(
        select(RuntimeImageAuthorization).where(
            RuntimeImageAuthorization.recipe_revision_id == recipe_revision_id,
            RuntimeImageAuthorization.receipt_id == receipt.id,
        )
    )
    if authorization is None:
        authorization = RuntimeImageAuthorization(
            recipe_revision_id=recipe_revision_id,
            receipt_id=receipt.id,
            **values,
            authorized_at=authorized_at,
            state="authorized",
        )
        session.add(authorization)
    else:
        if authorization.state != "authorized":
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_revoked",
                "current recipe runtime image authorization is not active",
            )
        existing = {key: getattr(authorization, key) for key in values}
        if existing != values:
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_conflict",
                "current recipe authorization identity changed",
            )
        authorization.authorized_at = authorized_at
        authorization.state = "authorized"
    session.flush()
    return authorization


def _validate_revision_reuse_identity(
    current: CatalogDocumentRevision,
    original: CatalogDocumentRevision,
) -> None:
    """Allow only editorial successors to reuse an immutable image receipt."""

    if current.execution_key is None or current.artifact_key is None:
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe execution and artifact identities are unavailable",
        )
    if (
        current.execution_key != original.execution_key
        or current.artifact_key != original.artifact_key
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.authorization_invalid",
            "current recipe execution or artifact identity changed",
        )
    current_projected = read_catalog_projection(current).model_dump(
        mode="json", exclude_none=True
    )
    original_projected = read_catalog_projection(original).model_dump(
        mode="json", exclude_none=True
    )
    for key in (
        "source_bundle_sha256",
        "build_model_artifacts",
        "build_topology_inputs",
        "build_resources",
        "build_security",
    ):
        if current_projected.get(key) != original_projected.get(key):
            raise RuntimeImagePreparationError(
                "runtime_image.authorization_invalid",
                "current recipe build inputs changed",
            )


class RuntimeImageStorage(Protocol):
    def read_receipt(self, archive_sha256: str) -> RuntimeImageReceipt:
        """Read the current receipt or raise a preparation error."""
        ...

    def prepare_path(self) -> Path:
        """Return a private path for a new export."""
        ...

    def commit(
        self,
        staged: Path,
        *,
        receipt: RuntimeImageReceipt,
    ) -> RuntimeImageReceipt:
        """Verify and atomically publish an archive and its receipt."""
        ...

    def verify_existing(
        self, archive_sha256: str, expected_bytes: int
    ) -> Path:
        """Return an existing verified archive or raise."""
        ...

    def find_published(
        self,
        registry_manifest_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceipt | None:
        """Find and verify a previously published image by immutable identity."""
        ...

    def find_verified(
        self,
        image_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceipt | None:
        """Find and verify a prepared image without invoking a transport."""
        ...


class FilesystemRuntimeImageStorage:
    """Content-addressed Controller/NAS storage under the OCI namespace.

    The caller supplies the shared Controller artifact root.  Runtime image
    archives and their receipts are kept in the explicit ``image-cache``
    namespace so model/build objects cannot be confused with OCI payloads.
    """

    def __init__(self, root: Path, *, maximum_bytes: int = 16 * 1024**4) -> None:
        self.root = root / IMAGE_CACHE_DIRECTORY
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
        existing_receipt: RuntimeImageReceipt | None = None
        if final.exists():
            existing_size = final.stat().st_size
            if existing_size != size:
                raise RuntimeImagePreparationError(
                    "runtime_image.archive_conflict", "content-addressed OCI archive conflicts"
                )
            receipt_path = self.root / f"{receipt.oci_archive_sha256}.receipt.json"
            if receipt_path.exists():
                try:
                    existing_receipt = self.read_receipt(receipt.oci_archive_sha256)
                except RuntimeImagePreparationError as error:
                    raise RuntimeImagePreparationError(
                        "runtime_image.archive_conflict",
                        "content-addressed OCI archive has no valid immutable receipt",
                    ) from error
            if existing_receipt is not None and existing_receipt.to_mapping() != receipt.to_mapping() and any(
                getattr(existing_receipt, field) != getattr(receipt, field)
                for field in (
                    "source",
                    "registry_manifest_digest",
                    "platform_manifest_digest",
                    "image_digest",
                    "oci_archive_sha256",
                    "image_bytes",
                    "local_image_config_id",
                    "architecture",
                    "runtime_interface",
                    "runtime_interface_label",
                    "build_id",
                )
            ):
                raise RuntimeImagePreparationError(
                    "runtime_image.archive_conflict",
                    "content-addressed OCI archive has a different immutable identity",
                )
            if staged != final:
                staged.unlink()
        else:
            os.replace(staged, final)
        if existing_receipt is not None:
            return existing_receipt
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
        if not 1 <= expected_bytes <= self.maximum_bytes or not verified_files.verify_path(
            path, archive_sha256, expected_bytes
        ):
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
        re-hashed when its filesystem identity changes, so a partial or corrupt object fails loudly
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
        lookup identities. Unchanged verified files do not need another byte scan.
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
    force: bool = False,
    progress: Callable[[str, int, int | None], None] | None = None,
) -> RuntimeImageReceipt:
    """Prepare the image selected by a canonical ``RecipeDefinition``.

    The recipe's execution image or successful build receipt is authoritative;
    ``runtime`` is the already compiled runtime projection and supplies the
    expected architecture/interface.  A catalog distribution document is
    deliberately not accepted as an authority here.
    """

    parsed = _canonical_recipe(recipe)
    projection = runtime_image_expectations(runtime)
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
            force=force,
            progress=progress,
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
    force: bool = False,
    progress: Callable[[str, int, int | None], None] | None = None,
) -> RuntimeImageReceipt:
    if not force:
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
            progress=progress,
        )
        _validate_evidence(
            evidence,
            expected_architecture,
            expected_interface_label,
            expected_requested_manifest=expected_manifest,
        )
        image_bytes, archive_sha = evidence.archive_bytes, evidence.archive_sha256
        architecture, runtime_interface, runtime_interface_label = (
            _receipt_runtime_identity(evidence, expected_interface)
        )
        receipt = RuntimeImageReceipt(
            schema_version=2,
            source="published",
            build_id=None,
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
            architecture=architecture,
            runtime_interface=runtime_interface,
            runtime_interface_label=runtime_interface_label,
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
    build_id = _string(value.get("build_id"), "runtime_image.build_id")
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
    try:
        cached = storage.read_receipt(archive_sha)
    except RuntimeImagePreparationError:
        # Rebuild absent or invalid metadata from the verified archive and
        # current build evidence. No alternate receipt shape is accepted.
        cached = None
    if (
        cached is not None
        and cached.source == "controller-build"
        and cached.image_digest == image_digest
        and cached.platform_manifest_digest == image_digest
        and cached.architecture == _wire_architecture(expected_architecture)
        and cached.runtime_interface == expected_interface
        and cached.runtime_interface_label == expected_interface_label
        and cached.image_bytes == image_bytes
        and cached.build_id == build_id
    ):
        return cached
    observed = transport.inspect_archive(
        existing,
        expected_architecture=expected_architecture,
        expected_runtime_interface=expected_interface_label,
        expected_archive_sha256=archive_sha,
        expected_archive_bytes=image_bytes,
    )
    _validate_evidence(
        observed,
        expected_architecture,
        expected_interface_label,
        expected_requested_manifest=None,
    )
    architecture, runtime_interface, runtime_interface_label = _receipt_runtime_identity(
        observed, expected_interface
    )
    receipt = RuntimeImageReceipt(
        schema_version=2,
        source="controller-build",
        distribution_publisher=publisher,
        distribution_slug=slug,
        distribution_content_sha256=content_sha256,
        registry_manifest_digest=_optional_string(value.get("registry_manifest_digest"), None),
        # The authenticated builder binds its original image manifest to this
        # exact archive checksum. Docker-save drops that original manifest;
        # Skopeo reconstructs a different manifest when reading the archive.
        # Preserve build provenance and independently inspect the archive's
        # actual config/platform, rather than comparing unrelated digests.
        platform_manifest_digest=image_digest,
        image_digest=image_digest,
        oci_archive_sha256=archive_sha,
        image_bytes=image_bytes,
        local_image_config_id=observed.config_id,
        # ``docker-archive:...`` is a Controller transport path, never a
        # runnable Spark image reference.
        local_image_reference=None,
        architecture=architecture,
        runtime_interface=runtime_interface,
        runtime_interface_label=runtime_interface_label,
        archive_path=str(getattr(storage, "root", Path("")) / archive_sha),
        recorded_at=_timestamp(now),
        build_id=build_id,
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


def runtime_image_expectations(value: Mapping[str, object] | object) -> dict[str, str]:
    """Read image verification expectations from the current compiler projection."""
    dump = getattr(value, "model_dump", None)
    raw: object = dump(mode="json") if callable(dump) else value
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid", "canonical runtime projection is unavailable"
        )
    if "runtime_interface" in raw:
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid", "runtime projection contains retired runtime_interface"
        )
    architecture = raw.get("architecture")
    interface = raw.get("interface")
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
        return _RUNTIME_ARCHITECTURE
    return value


def _receipt_runtime_identity(
    evidence: PulledImageEvidence, expected_interface: str
) -> tuple[RuntimeArchitecture, RuntimeInterface, RuntimeInterfaceLabel]:
    """Narrow validated transport evidence to the receipt's declared identity.

    ``_validate_evidence`` has already compared the evidence with the compiled
    runtime expectations; these checks make the same comparison explicit as the
    single supported literal set.  A mismatch is a validation failure, exactly
    as the receipt model previously reported it.
    """

    architecture = _wire_architecture(evidence.architecture)
    if architecture != _RUNTIME_ARCHITECTURE:
        raise ValueError("runtime image architecture is not supported")
    if expected_interface != _RUNTIME_INTERFACE:
        raise ValueError("runtime image interface is not supported")
    interface_label = evidence.runtime_interface
    if interface_label != _RUNTIME_INTERFACE_LABEL:
        raise ValueError("runtime image interface label is not supported")
    return architecture, expected_interface, interface_label


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


def _existing_bytes(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            # A native layer may be atomically renamed during sampling.
            pass
    return total


def _run_with_progress(
    command: list[str], sample: Callable[[], int],
    progress: Callable[[str, int, int | None], None] | None,
    phase: str, total: int | None,
) -> None:
    if progress is None:
        _run_text(command)
        return
    # The native helper continues transferring while the observer publishes
    # its sampled state. Database latency never blocks its receive loop.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="oci-transfer") as pool:
        transfer = pool.submit(_run_text, command)
        observed = 0
        while True:
            observed = max(observed, sample())
            progress(phase, min(observed, total) if total is not None else observed, total)
            try:
                transfer.result(timeout=1.0)
                break
            except TimeoutError:
                continue
        observed = max(observed, sample())
        progress(phase, min(observed, total) if total is not None else observed, total)


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
    "RuntimeArchitecture",
    "RuntimeImagePreparationError",
    "RuntimeImageReceipt",
    "RuntimeImageStorage",
    "RuntimeInterface",
    "RuntimeInterfaceLabel",
    "SkopeoOCIImageTransport",
    "persist_runtime_image_receipt",
    "prepare_runtime_image",
    "resolve_persisted_runtime_image_receipt",
    "runtime_image_expectations",
]
