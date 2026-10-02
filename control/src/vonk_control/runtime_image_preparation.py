"""Controller-owned preparation of exact runtime image archives.

This module is deliberately independent of Run/Switch orchestration.  It turns
a succeeded ``RecipeBuild`` receipt into a durable, content-verified image
receipt.  The transport only inspects the stored archive; it never receives a
Spark address and this module never uploads to a registry.  The filesystem
adapter is intentionally boring: archives are content addressed, and receipts
are replaced atomically after the archive has been verified.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.wire_model import Digest, WireModel
from vonk_forge_contracts import RecipeDefinition, document_sha256, read_recipe

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from .models import CatalogDocumentRevision, RecipeBuild, RuntimeImageAuthorization
from .oci_image_store import (
    DAMAGED_MANIFEST_CODES,
    IMAGE_CACHE_DIRECTORY,
    OciImageStore,
    OciImageStoreError,
    StoredImage,
)
from .runtime_adapters import (
    RuntimeAdapter,
    resolve_runtime_adapter,
)
from .validation_detail import validation_error_detail

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOGGER = logging.getLogger(__name__)
# One rejection detail is enough to name the rule; the document itself is
# never recorded and the rendered detail is already bounded per issue.
_MAX_RECEIPT_REJECTION_DETAIL = 512


class RuntimeImagePreparationError(ValueError):
    """A canonical identity, image archive, or receipt is invalid.

    ``retryable`` and ``recovery_actions`` mirror the operator-facing failure
    contract the availability service consumes, so a transient preparation
    failure is rescheduled instead of reported as terminal.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        recovery_actions: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.recovery_actions = tuple(recovery_actions)
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class PulledImageEvidence:
    """Evidence returned by the Controller's OCI archive inspection."""

    manifest_digest: str
    config_id: str
    local_reference: str
    architecture: str
    runtime_interface: str
    archive_sha256: str
    archive_bytes: int


class OCIImageTransport(Protocol):
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
            raise RuntimeImagePreparationError(
                "runtime_image.inspect_invalid",
                "stored OCI image manifest or config is unreadable",
                retryable=True,
            ) from error
        if not isinstance(config, Mapping):
            raise RuntimeImagePreparationError(
                "runtime_image.inspect_invalid", "stored OCI image config is invalid"
            )
        architecture = _observed_architecture(config)
        if architecture != expected_architecture:
            raise RuntimeImagePreparationError(
                "runtime_image.architecture_mismatch",
                "OCI image architecture does not match the recipe",
            )
        interface = _observed_runtime_interface(config)
        if interface != expected_runtime_interface:
            raise RuntimeImagePreparationError(
                "runtime_image.interface_mismatch",
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
    distribution_publisher: str = Field(min_length=1, max_length=128)
    distribution_slug: str = Field(min_length=1, max_length=128)
    distribution_content_sha256: Digest
    image_digest: ImageDigest
    oci_archive_sha256: Digest
    image_bytes: int = Field(strict=True, ge=1, le=16 * 1024**4)
    local_image_config_id: ImageDigest
    architecture: RuntimeArchitecture
    runtime_interface: RuntimeInterface
    archive_path: str = Field(min_length=1, max_length=4096)
    recorded_at: str = Field(min_length=1, max_length=128)
    build_id: str = Field(min_length=1, max_length=128)
    # The executable build identity is the reuse key: the filesystem receipt
    # carries it so a prepared build can be recognized without reading the SQL
    # build index.
    build_input_sha256: Digest | None = None
    runtime_interface_label: RuntimeInterfaceLabel
    # The resolved platform adaptation identity, keyed to the final image
    # digest beside it. ``runtime-interface="v1"`` only marks compatibility;
    # these two fields identify which adapter implementation produced the
    # bytes, so an adapter change cannot read as the same prepared image.
    runtime_adapter: str = Field(min_length=1, max_length=128)
    runtime_adapter_sha256: Digest

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class RuntimeImageReferenceIntent(WireModel):
    """Exact SQL coordination identity for an image archive publication.

    This intent protects an archive while the existing availability owner is
    publishing it. It is not evidence that managed bytes or a receipt exist;
    those facts remain owned by ``RuntimeImageStorage``.
    """

    schema_version: Literal[2]
    operation_id: str = Field(min_length=1, max_length=128)
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(strict=True, ge=1)
    claim_owner: str = Field(min_length=1, max_length=200)
    oci_archive_sha256: Digest
    image_digest: ImageDigest
    image_bytes: int = Field(strict=True, ge=1, le=16 * 1024**4)

    def belongs_to(
        self,
        *,
        operation_id: str,
        recipe_revision_id: str,
        attempt: int,
        claim_owner: str,
    ) -> bool:
        return (
            self.operation_id == operation_id
            and self.recipe_revision_id == recipe_revision_id
            and self.attempt == attempt
            and self.claim_owner == claim_owner
        )


def read_runtime_image_reference_intent(
    value: object,
) -> RuntimeImageReferenceIntent:
    """Validate a decoded SQL JSON value through its canonical wire model."""

    try:
        return RuntimeImageReferenceIntent.model_validate_json(
            canonical_message(value), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise RuntimeImagePreparationError(
            "runtime_image.reference_intent_invalid",
            "runtime image reference intent is malformed",
        ) from error


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
            "runtime image receipt identity is unavailable or malformed"
            + validation_error_detail(error),
        ) from error


class _ReceiptDocumentRejected(Exception):
    """A present receipt file the current contract cannot parse.

    Deliberately not a ``RuntimeImagePreparationError``: a scan over all
    receipts treats the file as one archive's stale metadata and skips it,
    while an exact-identity read of that archive still reports it.  It carries
    the contract code and bounded detail that rejected the document, never a
    field read out of it.
    """

    def __init__(self, code: str, detail: str, *, own_stale: bool = True) -> None:
        self.code = code
        self.detail = detail
        # False when the document may belong to a newer receipt contract
        # (a higher or unknown schema version, or fields this contract does
        # not know). Such a receipt is never deleted or overwritten here: a
        # newer Controller in a mixed deploy may still rely on it.
        self.own_stale = own_stale
        super().__init__(detail)


# Schema-2 fields this contract retired without a version bump. A receipt
# whose only unknown fields are these is this Controller's own older document.
_RETIRED_RECEIPT_FIELDS = frozenset(
    {"local_image_reference", "platform_manifest_digest"}
)


def _receipt_may_be_newer(value: object, error: BaseException | None) -> bool:
    """Whether a rejected receipt document may come from a newer contract."""

    if not isinstance(value, Mapping):
        return False
    version = value.get("schema_version")
    if type(version) is not int:
        return "schema_version" in value
    if version > 2:
        return True
    if version < 2:
        return False
    if isinstance(error, ValidationError):
        return any(
            item.get("type") == "extra_forbidden"
            and item.get("loc", ("",))[0] not in _RETIRED_RECEIPT_FIELDS
            for item in error.errors()
        )
    return False


def _load_receipt_document(path: Path) -> RuntimeImageReceipt:
    """Read one stored receipt file under the current contract.

    An unreadable file -- including a denied read -- raises
    ``RuntimeImagePreparationError``: an access failure is never a scan miss
    and remains an explicit blocker.  A document that is present but does not
    satisfy the current contract raises ``_ReceiptDocumentRejected`` naming the
    rule that rejected it.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_unavailable",
            "runtime image receipt could not be read",
        ) from error
    except UnicodeDecodeError as error:
        raise _ReceiptDocumentRejected(
            "runtime_image.receipt_unavailable",
            "runtime image receipt is not valid UTF-8 JSON",
        ) from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise _ReceiptDocumentRejected(
            "runtime_image.receipt_unavailable",
            "runtime image receipt identity is unavailable or malformed",
        ) from error
    try:
        return _parse_runtime_image_receipt(document)
    except RuntimeImagePreparationError as error:
        raise _ReceiptDocumentRejected(
            error.code,
            error.detail,
            own_stale=not _receipt_may_be_newer(document, error.__cause__),
        ) from error
    except (TypeError, ValueError) as error:
        raise _ReceiptDocumentRejected(
            "runtime_image.receipt_unavailable",
            "runtime image receipt identity is unavailable or malformed",
            own_stale=not _receipt_may_be_newer(document, error),
        ) from error


def _log_rejected_receipt(path: Path, rejection: _ReceiptDocumentRejected) -> None:
    """Record the bounded rule that rejected one stored receipt file.

    The archive digest and the rejecting contract rule are diagnostic; the
    unreadable document is never carried into the record.
    """

    _LOGGER.warning(
        "runtime image receipt %s rejected by %s: %s",
        path.name.removesuffix(".receipt.json"),
        rejection.code,
        rejection.detail[:_MAX_RECEIPT_REJECTION_DETAIL],
    )


def prefixed_image_digest(value: str | None) -> str | None:
    """Return a ``sha256:``-prefixed image digest.

    The managed-storage receipt stores image digests without the algorithm
    prefix, while Controller SQL stores them with it. One normalization keeps a
    storage observation comparable to a durable authorization without
    duplicating either spelling at every call site.
    """

    if value is None:
        return None
    return value if value.startswith("sha256:") else f"sha256:{value}"


def record_runtime_image_reference(
    session: Session,
    *,
    recipe_revision_id: str,
    effective_execution_key: str,
    receipt: RuntimeImageReceipt,
    recorded_at: datetime,
) -> None:
    """Index which verified archive a revision runs, for retention and the library.

    The image was accepted at ingress and its receipt lives in managed storage.
    This row only tells garbage collection and the library which archive a
    revision uses. Nothing reads it as a permission: an image is identified by
    its content and any revision whose build input matches may run it.
    """

    try:
        require_reference_open(
            session,
            (ArtifactIdentity("runtime-image", receipt.oci_archive_sha256),),
            now=recorded_at,
        )
    except ArtifactLifecycleError as error:
        raise RuntimeImagePreparationError(
            error.code, error.detail, retryable=error.retryable
        ) from error
    known = session.scalar(
        select(RuntimeImageAuthorization.id).where(
            RuntimeImageAuthorization.recipe_revision_id == recipe_revision_id,
            RuntimeImageAuthorization.effective_execution_key
            == effective_execution_key,
            RuntimeImageAuthorization.oci_archive_sha256 == receipt.oci_archive_sha256,
        )
    )
    if known is None:
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=recipe_revision_id,
                original_content_digest=receipt.distribution_content_sha256,
                effective_execution_key=effective_execution_key,
                image_digest=prefixed_image_digest(receipt.image_digest),
                local_image_config_id=prefixed_image_digest(
                    receipt.local_image_config_id
                ),
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=receipt.build_id,
                authorized_at=recorded_at,
                state="authorized",
            )
        )
        session.flush()


class RuntimeImageStorage(Protocol):
    def read_receipt(self, archive_sha256: str) -> RuntimeImageReceipt:
        """Read the current receipt or raise a preparation error."""
        ...

    def publication_lock(self, archive_sha256: str) -> AbstractContextManager[None]:
        """Claim the exact output archive's publication fence without waiting."""
        ...

    def commit(
        self,
        staged: Path,
        *,
        receipt: RuntimeImageReceipt,
    ) -> RuntimeImageReceipt:
        """Publish the receipt of an image already in the layered store."""
        ...

    def existing_archive(self, archive_sha256: str, expected_bytes: int) -> Path:
        """Return an existing published archive (identity and size) or raise."""
        ...

    def published_archive_bytes(self, archive_sha256: str) -> int:
        """Read the exact published archive size without following symlinks."""
        ...

    def remove_published(self, archive_sha256: str) -> int:
        """Remove one archive and receipt; caller holds its publication lock."""
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

    def find_build(
        self,
        build_input_sha256: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str | None = None,
    ) -> RuntimeImageReceipt | None:
        """Find and verify a prepared Controller build by its exact input identity."""
        ...


_IMAGE_CONTENT_FIELDS = (
    "image_digest",
    "oci_archive_sha256",
    "image_bytes",
    "local_image_config_id",
    "architecture",
    "runtime_interface",
    "runtime_interface_label",
)


def _same_image(stored: RuntimeImageReceipt, observed: RuntimeImageReceipt) -> bool:
    """Whether two receipts describe the same image bytes.

    Build, build input and runtime adapter say who asked for the image, not
    what it is, so they are not compared.
    """

    return all(
        getattr(stored, field) == getattr(observed, field)
        for field in _IMAGE_CONTENT_FIELDS
    )


def _with_provenance(
    stored: RuntimeImageReceipt,
    *,
    build_id: str,
    build_input_sha256: str | None,
    runtime_adapter: str,
    runtime_adapter_sha256: str,
) -> RuntimeImageReceipt:
    return RuntimeImageReceipt(
        **{
            **stored.to_mapping(),
            "build_id": build_id,
            "build_input_sha256": build_input_sha256 or stored.build_input_sha256,
            "runtime_adapter": runtime_adapter,
            "runtime_adapter_sha256": runtime_adapter_sha256,
        }
    )


class FilesystemRuntimeImageStorage:
    """Content-addressed Controller/NAS storage under the OCI namespace.

    The caller supplies the shared Controller artifact root.  Runtime image
    archives and their receipts are kept in the explicit ``image-cache``
    namespace so model/build objects cannot be confused with OCI payloads.
    """

    def __init__(self, root: Path, *, maximum_bytes: int = 16 * 1024**4) -> None:
        self.root = root / IMAGE_CACHE_DIRECTORY
        self.layout = OciImageStore(root)
        self.maximum_bytes = maximum_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        # Receipts this process already reported, so a scan does not repeat
        # the same warning for a file it could not change.
        self._reported_receipts: set[tuple[str, int]] = set()
        self._reported_damaged: set[str] = set()

    @contextmanager
    def publication_lock(self, archive_sha256: str) -> Iterator[None]:
        """Serialize reference fencing and atomic publication for one archive."""

        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.identity_invalid",
                "publication lock requires an exact archive SHA-256",
            )
        lock_root = self.root / ".publication-locks"
        try:
            lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory_fd = os.open(
                lock_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            raise RuntimeImagePreparationError(
                "runtime_image.lock_unavailable",
                "managed image publication lock directory is unavailable",
            ) from error
        try:
            descriptor = os.open(
                f"{archive_sha256}.lock",
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as error:
            os.close(directory_fd)
            raise RuntimeImagePreparationError(
                "runtime_image.lock_unavailable",
                "managed image publication lock file is unavailable",
            ) from error
        os.close(directory_fd)
        with os.fdopen(descriptor, "a+b") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise RuntimeImagePreparationError(
                    "runtime_image.lock_unavailable",
                    "managed image publication lock is not a regular file",
                )
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeImagePreparationError(
                    "runtime_image.publication_contended",
                    "waiting for another owner to finish this image publication",
                    retryable=True,
                    recovery_actions=("retry",),
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def commit(
        self, staged: Path, *, receipt: RuntimeImageReceipt
    ) -> RuntimeImageReceipt:
        """Publish the receipt of an image already in the layered store.

        ``staged`` is the image's manifest blob in the layout; nothing is
        moved. An existing receipt for the same image is kept, completed, or
        refused when it binds the image to another identity.
        """
        final = self.existing_archive(receipt.oci_archive_sha256, receipt.image_bytes)
        if staged != final:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch",
                "runtime image receipt names another stored image",
            )
        receipt_path = self.root / f"{receipt.oci_archive_sha256}.receipt.json"
        existing_receipt: RuntimeImageReceipt | None = None
        if receipt_path.exists():
            try:
                existing_receipt = _load_receipt_document(receipt_path)
            except _ReceiptDocumentRejected as rejection:
                # Stale metadata about these exact verified blobs is replaced
                # below. A receipt that may belong to a newer contract is left
                # in place: this preparation retries until the deploy converges.
                _log_rejected_receipt(receipt_path, rejection)
                if not rejection.own_stale:
                    raise RuntimeImagePreparationError(
                        "runtime_image.receipt_contract_newer",
                        "runtime image receipt was written by a newer Controller contract",
                        retryable=True,
                        recovery_actions=("retry",),
                    ) from rejection
                existing_receipt = None
        if existing_receipt is not None and _same_image(existing_receipt, receipt):
            # The same image bytes under another build (a sibling recipe that
            # publishes the same prebuilt image, an editorial revision) are
            # the same image. The stored receipt keeps the content; the build
            # and adapter that ask for it are provenance and travel with the
            # answer, never a reason to refuse.
            if (
                existing_receipt.build_input_sha256 is None
                and receipt.build_input_sha256 is not None
            ):
                existing_receipt = RuntimeImageReceipt(
                    **{
                        **existing_receipt.to_mapping(),
                        "build_input_sha256": receipt.build_input_sha256,
                    }
                )
                _atomic_json_replace(receipt_path, existing_receipt.to_mapping())
            return _with_provenance(
                existing_receipt,
                build_id=receipt.build_id,
                build_input_sha256=receipt.build_input_sha256,
                runtime_adapter=receipt.runtime_adapter,
                runtime_adapter_sha256=receipt.runtime_adapter_sha256,
            )
        # No receipt, or one whose content disagrees with what was just
        # observed in the stored bytes: the observation wins and is recorded.
        published = RuntimeImageReceipt(
            **{**receipt.to_mapping(), "archive_path": str(final)}
        )
        _atomic_json_replace(receipt_path, published.to_mapping())
        return published

    def existing_archive(self, archive_sha256: str, expected_bytes: int) -> Path:
        """The stored image's manifest blob, when the whole image is present.

        ``archive_sha256`` is the image's address in the layered store (its
        manifest digest hex) and ``expected_bytes`` its stored size. The
        blobs were verified when they entered the layout; reuse checks names
        and sizes only.
        """
        image = self._stored_image(archive_sha256)
        if image is None:
            raise RuntimeImagePreparationError(
                "runtime_image.cache_missing",
                "runtime image is not present in Controller storage",
                retryable=True,
            )
        if image.stored_bytes != expected_bytes:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch",
                "stored runtime image does not match its recorded size",
            )
        return self.layout.blob_path(image.manifest_digest)

    def _stored_image(self, archive_sha256: str) -> StoredImage | None:
        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_invalid", "runtime image address is invalid"
            )
        try:
            return self.layout.read(f"sha256:{archive_sha256}")
        except OciImageStoreError as error:
            if error.code in DAMAGED_MANIFEST_CODES:
                # A manifest whose content is damaged is cache loss, like a
                # missing one: the next preparation stores the image again
                # (which rewrites the manifest and reuses intact layers).
                if archive_sha256 not in self._reported_damaged:
                    self._reported_damaged.add(archive_sha256)
                    _LOGGER.warning(
                        "stored runtime image %s has a damaged manifest and will "
                        "be stored again when it is next needed: %s",
                        archive_sha256,
                        error.detail,
                    )
                return None
            raise RuntimeImagePreparationError(
                "runtime_image.archive_unavailable", error.detail, retryable=True
            ) from error

    def published_archive_bytes(self, archive_sha256: str) -> int:
        """The stored size of one image (shared layers included), or 0."""

        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.identity_invalid",
                "image size lookup requires an exact SHA-256",
            )
        image = self._stored_image(archive_sha256)
        return 0 if image is None else image.stored_bytes

    def remove_published(self, archive_sha256: str) -> int:
        """Retire one image's receipt by exact digest under the held lock.

        The caller owns the matching nonblocking ``publication_lock`` and its
        committed SQL deletion fence. The image's blobs may be shared with
        other images; garbage collection reclaims the ones no image
        references, so this reclaims no bytes itself. Repeating it is safe.
        """

        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.identity_invalid",
                "image removal requires an exact SHA-256",
            )
        try:
            (self.root / f"{archive_sha256}.receipt.json").unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeImagePreparationError(
                "runtime_image.removal_storage_failed",
                "runtime image receipt could not be removed safely",
                retryable=True,
                recovery_actions=("retry",),
            ) from error
        return 0

    def build_archive_available(self, archive_sha256: str, expected_bytes: int) -> bool:
        """Whether the whole image is in the layered store, by name and size."""

        if (
            _SHA256.fullmatch(archive_sha256) is None
            or type(expected_bytes) is not int
            or not 1 <= expected_bytes <= self.maximum_bytes
        ):
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_invalid",
                "source-build image evidence is invalid",
            )
        image = self._stored_image(archive_sha256)
        if image is None:
            return False
        if image.stored_bytes != expected_bytes:
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch",
                "stored runtime image size changed",
            )
        return True

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
        A receipt whose archive is absent is a miss, not a failure: the caller
        re-prepares rather than surfacing an integrity error.
        """

        if _IMAGE_DIGEST.fullmatch(image_digest) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_invalid", "runtime image identity is invalid"
            )
        expected_architecture = _wire_architecture(expected_architecture)
        expected_label = _runtime_interface_label(expected_runtime_interface)
        for receipt in self._iter_receipts():
            if receipt.image_digest != image_digest:
                continue
            if (
                receipt.architecture != expected_architecture
                or receipt.runtime_interface != expected_runtime_interface
                or receipt.runtime_interface_label != expected_label
            ):
                # Not a proof of the requested identity: a miss, so the
                # caller prepares the image again.
                continue
            if not self._archive_is_present(receipt):
                continue
            return receipt
        return None

    def find_build(
        self,
        build_input_sha256: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str | None = None,
    ) -> RuntimeImageReceipt | None:
        """Return the prepared Controller build for an exact input identity.

        This is the reuse gate for source builds.  The filesystem receipt is
        the authority for "the bytes are already here": a matching receipt
        whose archive is absent is ordinary cache loss and returns ``None`` so
        the caller builds again.  Only the recorded executable input identity
        selects a receipt; a receipt that cannot prove its inputs is not reused.
        """

        if _SHA256.fullmatch(build_input_sha256) is None:
            raise RuntimeImagePreparationError(
                "runtime_image.digest_invalid", "build input identity is invalid"
            )
        expected_architecture = _wire_architecture(expected_architecture)
        expected_label = _runtime_interface_label(expected_runtime_interface)
        receipts = (
            self._iter_receipts()
            if expected_archive_sha256 is None
            else (self.read_receipt(expected_archive_sha256),)
        )
        for receipt in receipts:
            if receipt.build_input_sha256 != build_input_sha256:
                continue
            if (
                receipt.architecture != expected_architecture
                or receipt.runtime_interface != expected_runtime_interface
                or receipt.runtime_interface_label != expected_label
            ):
                # Not a proof of the requested identity: a miss, so the
                # caller prepares the image again.
                continue
            if not self._archive_is_present(receipt):
                continue
            return receipt
        return None

    def _iter_receipts(self) -> Iterable[RuntimeImageReceipt]:
        """Yield every receipt the current contract can parse.

        A scan spans unrelated archives and recipes, so a file this contract
        cannot parse is one archive's stale metadata: skip it with a bounded
        warning naming its digest instead of failing every lookup that happens
        to walk past it.  ``read_receipt`` for that exact digest stays strict.
        An unreadable file is not a parse failure and still raises.
        """

        for receipt_path in sorted(self.root.glob("*.receipt.json")):
            try:
                yield _load_receipt_document(receipt_path)
            except _ReceiptDocumentRejected as rejection:
                self._discard_rejected_receipt(receipt_path, rejection)

    def _discard_rejected_receipt(
        self, receipt_path: Path, rejection: _ReceiptDocumentRejected
    ) -> None:
        """Delete one of our own receipts that the current contract rejects.

        The receipt is derived metadata about content-addressed bytes; without
        it the next preparation re-derives a current receipt (or rebuilds), so
        a stale document is removed once instead of being logged on every scan.
        A receipt being published concurrently is left for a later scan.
        """

        archive_sha256 = receipt_path.name.removesuffix(".receipt.json")
        if not rejection.own_stale or _SHA256.fullmatch(archive_sha256) is None:
            if self._first_report(receipt_path):
                _log_rejected_receipt(receipt_path, rejection)
            return
        try:
            with self.publication_lock(archive_sha256):
                try:
                    _load_receipt_document(receipt_path)
                    return
                except _ReceiptDocumentRejected as current:
                    if not current.own_stale:
                        return
                receipt_path.unlink(missing_ok=True)
        except (RuntimeImagePreparationError, OSError) as error:
            # Say why the stale file stays, once: the cause (a lock or file
            # this process may not change) is the operator's next action.
            if self._first_report(receipt_path):
                cause = error.__cause__ if error.__cause__ is not None else error
                _LOGGER.warning(
                    "could not discard stale runtime image receipt %s rejected by "
                    "%s: %s; %s: %s",
                    archive_sha256,
                    rejection.code,
                    rejection.detail[:_MAX_RECEIPT_REJECTION_DETAIL],
                    getattr(error, "code", type(error).__name__),
                    cause,
                )
            return
        _LOGGER.warning(
            "discarded runtime image receipt %s rejected by %s: %s",
            archive_sha256,
            rejection.code,
            rejection.detail[:_MAX_RECEIPT_REJECTION_DETAIL],
        )

    def _first_report(self, receipt_path: Path) -> bool:
        """Whether this process has not yet reported this receipt version."""

        try:
            version = receipt_path.stat().st_mtime_ns
        except OSError:
            version = 0
        key = (receipt_path.name, version)
        if key in self._reported_receipts:
            return False
        self._reported_receipts.add(key)
        return True

    def _archive_is_present(self, receipt: RuntimeImageReceipt) -> bool:
        """Report archive presence; clean absence is a miss, not a failure."""

        try:
            self.existing_archive(receipt.oci_archive_sha256, receipt.image_bytes)
        except RuntimeImagePreparationError as error:
            if error.code == "runtime_image.cache_missing":
                return False
            raise
        return True

    def read_receipt(self, archive_sha256: str) -> RuntimeImageReceipt:
        """Read the exact receipt for one named archive; never a scan miss.

        A document the current contract cannot parse is reported here rather
        than skipped, so an exact-identity read cannot silently degrade into
        "no receipt".
        """

        path = self.root / f"{archive_sha256}.receipt.json"
        try:
            return _load_receipt_document(path)
        except _ReceiptDocumentRejected as rejection:
            raise RuntimeImagePreparationError(
                rejection.code, rejection.detail
            ) from rejection


def prepare_runtime_image(
    recipe: RecipeDefinition | Mapping[str, object] | object,
    *,
    runtime: Mapping[str, object] | object,
    storage: RuntimeImageStorage,
    build_receipt: Mapping[str, object] | object,
    transport: OCIImageTransport | None = None,
    now: datetime | None = None,
    receipt_writer: Callable[[RuntimeImageReceipt], object] | None = None,
    before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
) -> RuntimeImageReceipt:
    """Prepare the image a succeeded build produced for a ``RecipeDefinition``.

    The build receipt is authoritative; ``runtime`` is the already compiled
    runtime projection and supplies the expected architecture/interface.  A
    catalog distribution document is deliberately not accepted as an
    authority here.
    """

    parsed = _canonical_recipe(recipe)
    projection = runtime_image_expectations(runtime)
    receipt = _prepare_from_build(
        build_receipt,
        storage=storage,
        transport=transport or OciLayoutImageTransport(),
        publisher=parsed.identity.publisher,
        slug=parsed.identity.slug,
        content_sha256=_recipe_digest(recipe),
        expected_architecture=projection["architecture"],
        expected_interface=projection["interface"],
        adapter=resolve_runtime_adapter(parsed.runtime.engine, parsed.topology),
        now=now,
        before_publish=before_publish,
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


class RuntimeImagePreparer(Protocol):
    def __call__(
        self,
        document: Mapping[str, object],
        runtime_spec: Mapping[str, object],
        build: RecipeBuild | None,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ) -> RuntimeImageReceipt:
        """Prepare and authorize a receipt, with optional owner fencing."""
        ...


def make_runtime_image_receipt_preparer(
    sessions: sessionmaker[Session],
    storage: RuntimeImageStorage,
    transport: OCIImageTransport,
    *,
    clock: Callable[[], datetime],
) -> RuntimeImagePreparer:
    """Build the production callback that prepares and authorizes an image.

    API and worker composition share this callback so recovery and build
    execution use the same receipt persistence boundary.
    """

    def prepare(
        document: Mapping[str, object],
        runtime_spec: Mapping[str, object],
        build: RecipeBuild | None,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ) -> RuntimeImageReceipt:
        runtime = runtime_spec.get("runtime")
        if not isinstance(runtime, Mapping):
            raise TypeError("compiled runtime projection is unavailable")
        identity = runtime_spec.get("identity")
        effective_execution_key = (
            identity.get("execution_sha256") if isinstance(identity, Mapping) else None
        )
        if not isinstance(effective_execution_key, str):
            raise TypeError("compiled runtime execution identity is unavailable")

        if build is None:
            raise ValueError("source build receipt is unavailable")
        build_receipt = {
            "state": build.state,
            "build_id": build.id,
            "build_input_sha256": build.build_input_sha256,
            "image_digest": build.image_digest,
            "oci_layout_sha256": build.oci_layout_sha256,
            "image_bytes": build.image_bytes,
        }

        recipe_digest = _recipe_digest(document)

        def write_receipt(receipt: RuntimeImageReceipt) -> None:
            with sessions.begin() as session:
                revision = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.content_digest == recipe_digest,
                    )
                )
                if revision is not None:
                    record_runtime_image_reference(
                        session,
                        recipe_revision_id=revision.id,
                        effective_execution_key=effective_execution_key,
                        receipt=receipt,
                        recorded_at=clock(),
                    )

        return prepare_runtime_image(
            document,
            runtime=runtime,
            storage=storage,
            transport=transport,
            build_receipt=build_receipt,
            now=clock(),
            receipt_writer=write_receipt,
            before_publish=before_publish,
        )

    return prepare


def stored_runtime_image_resolver(
    storage: RuntimeImageStorage,
) -> Callable[[object, str, object], RuntimeImageReceipt]:
    """Resolve a compiled image to the verified archive managed storage holds.

    The image is identified by its content: no recipe revision is consulted.
    A missing archive is a wait for the ordinary preparation phase, never a
    refusal.
    """

    def resolve(
        _document: object, image_digest: str, runtime_spec: object
    ) -> RuntimeImageReceipt:
        runtime = (
            runtime_spec.get("runtime") if isinstance(runtime_spec, Mapping) else None
        )
        if not isinstance(runtime, Mapping):
            raise TypeError(
                "runtime image preparation is required: runtime projection is unavailable"
            )
        expectations = runtime_image_expectations(runtime)
        receipt = storage.find_verified(
            image_digest,
            expected_architecture=expectations["architecture"],
            expected_runtime_interface=expectations["interface"],
        )
        if receipt is None:
            raise ValueError(
                "runtime image preparation is required before compile/install"
            )
        return receipt

    return resolve


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
    adapter: RuntimeAdapter,
    now: datetime | None,
    before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
) -> RuntimeImageReceipt:
    value = _object_mapping(raw)
    if value.get("state") != "succeeded":
        raise RuntimeImagePreparationError(
            "runtime_image.build_incomplete", "source-build receipt is not succeeded"
        )
    image_digest = _string(value.get("image_digest"), "runtime_image.build_digest")
    build_id = _string(value.get("build_id"), "runtime_image.build_id")
    archive_sha = _string(
        value.get("oci_layout_sha256"), "runtime_image.build_archive_digest"
    )
    if (
        _IMAGE_DIGEST.fullmatch(image_digest) is None
        or _SHA256.fullmatch(archive_sha) is None
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid", "source-build image evidence is invalid"
        )
    image_bytes = value.get("image_bytes")
    if type(image_bytes) is not int or image_bytes < 1:
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid", "source-build image bytes are invalid"
        )
    raw_build_input = value.get("build_input_sha256")
    if raw_build_input is not None and (
        not isinstance(raw_build_input, str)
        or _SHA256.fullmatch(raw_build_input) is None
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.receipt_invalid", "source-build input identity is invalid"
        )
    existing = storage.existing_archive(archive_sha, image_bytes)
    expected_interface_label = _runtime_interface_label(expected_interface)
    try:
        cached = storage.read_receipt(archive_sha)
    except RuntimeImagePreparationError:
        # Rebuild absent or invalid metadata from the verified archive and
        # current build evidence. No alternate receipt shape is accepted.
        cached = None
    if (
        cached is not None
        and cached.image_digest == image_digest
        and cached.architecture == _wire_architecture(expected_architecture)
        and cached.runtime_interface == expected_interface
        and cached.runtime_interface_label == expected_interface_label
        and cached.image_bytes == image_bytes
    ):
        answer = _with_provenance(
            cached,
            build_id=build_id,
            build_input_sha256=raw_build_input,
            runtime_adapter=adapter.adapter_id,
            runtime_adapter_sha256=adapter.digest,
        )
        with storage.publication_lock(cached.oci_archive_sha256):
            if before_publish is not None:
                before_publish(answer)
            return storage.commit(existing, receipt=answer)
    observed = transport.inspect_archive(
        existing,
        expected_architecture=expected_architecture,
        expected_runtime_interface=expected_interface_label,
        expected_archive_sha256=archive_sha,
        expected_archive_bytes=image_bytes,
    )
    _validate_evidence(observed, expected_architecture, expected_interface_label)
    architecture, runtime_interface, runtime_interface_label = (
        _receipt_runtime_identity(observed, expected_interface)
    )
    # A caller that only knows the archive (for example a pre-upgrade build
    # row) must not erase an input identity the existing receipt already
    # proves; a fresh evidence mapping records its own.
    recorded_build_input = (
        raw_build_input
        if raw_build_input is not None
        else (cached.build_input_sha256 if cached is not None else None)
    )
    receipt = RuntimeImageReceipt(
        schema_version=2,
        distribution_publisher=publisher,
        distribution_slug=slug,
        distribution_content_sha256=content_sha256,
        # The authenticated builder binds its original image manifest to this
        # exact archive checksum. Docker-save drops that original manifest;
        # Skopeo reconstructs a different manifest when reading the archive.
        # Preserve build provenance and independently inspect the archive's
        # actual config/platform, rather than comparing unrelated digests.
        image_digest=image_digest,
        oci_archive_sha256=archive_sha,
        image_bytes=image_bytes,
        local_image_config_id=observed.config_id,
        architecture=architecture,
        runtime_interface=runtime_interface,
        runtime_interface_label=runtime_interface_label,
        archive_path=str(getattr(storage, "root", Path("")) / archive_sha),
        recorded_at=_timestamp(now),
        build_id=build_id,
        build_input_sha256=recorded_build_input,
        runtime_adapter=adapter.adapter_id,
        runtime_adapter_sha256=adapter.digest,
    )
    # A build receipt already points at an immutable stored archive, but still
    # update the receipt atomically.
    with storage.publication_lock(receipt.oci_archive_sha256):
        if before_publish is not None:
            before_publish(receipt)
        return storage.commit(existing, receipt=receipt)


def _canonical_recipe(
    value: RecipeDefinition | Mapping[str, object] | object,
) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    raw = getattr(value, "document", value)
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.recipe_invalid",
            "canonical RecipeDefinition document is unavailable",
        )
    try:
        return read_recipe(raw)
    except Exception as error:
        raise RuntimeImagePreparationError(
            "runtime_image.recipe_invalid",
            "recipe does not satisfy canonical RecipeDefinition",
        ) from error


def runtime_image_expectations(value: Mapping[str, object] | object) -> dict[str, str]:
    """Read image verification expectations from the current compiler projection."""
    dump = getattr(value, "model_dump", None)
    raw: object = dump(mode="json") if callable(dump) else value
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid",
            "canonical runtime projection is unavailable",
        )
    if "runtime_interface" in raw:
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid",
            "runtime projection contains retired runtime_interface",
        )
    architecture = raw.get("architecture")
    interface = raw.get("interface")
    if (
        not isinstance(architecture, str)
        or not architecture
        or not isinstance(interface, str)
        or not interface
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.runtime_invalid",
            "runtime projection lacks observed architecture/interface expectations",
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


def _recipe_digest(value: object) -> str:
    """Return the stored digest of a published recipe document."""

    digest = getattr(value, "content_digest", None)
    if isinstance(digest, str):
        return digest
    raw = getattr(value, "document", value)
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationError(
            "runtime_image.recipe_invalid",
            "published recipe document is unavailable",
        )
    return document_sha256(raw)


def _validate_evidence(
    evidence: PulledImageEvidence,
    expected_architecture: str,
    expected_interface: str | None,
) -> None:
    if not isinstance(evidence, PulledImageEvidence):
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid", "OCI transport returned invalid evidence"
        )
    if _IMAGE_DIGEST.fullmatch(evidence.manifest_digest) is None:
        raise RuntimeImagePreparationError(
            "runtime_image.digest_mismatch",
            "OCI transport returned a different manifest digest",
        )
    if (
        _IMAGE_DIGEST.fullmatch(evidence.config_id) is None
        or not evidence.local_reference
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid",
            "OCI transport did not return local image identity",
        )
    if (
        _SHA256.fullmatch(evidence.archive_sha256) is None
        or type(evidence.archive_bytes) is not int
        or evidence.archive_bytes < 1
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.evidence_invalid",
            "OCI transport did not return archive verification evidence",
        )
    if evidence.architecture != expected_architecture:
        raise RuntimeImagePreparationError(
            "runtime_image.architecture_mismatch",
            "verified image architecture does not match the recipe",
        )
    if not evidence.runtime_interface or (
        expected_interface is not None
        and evidence.runtime_interface != expected_interface
    ):
        raise RuntimeImagePreparationError(
            "runtime_image.interface_mismatch",
            "verified image runtime interface does not match the recipe",
        )


def _run_json_text(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeImagePreparationError(
            "runtime_image.inspect_invalid", "stored OCI manifest is invalid JSON"
        ) from error


def _observed_architecture(image: Mapping[str, object]) -> str:
    os_name, architecture = (
        image.get("os", image.get("Os")),
        image.get("architecture", image.get("Architecture")),
    )
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
            "runtime_image.interface_missing",
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
        raise RuntimeImagePreparationError(
            "runtime_image.interface_missing",
            "OCI image runtime interface label is missing or ambiguous",
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
            "state",
            "image_digest",
            "oci_layout_sha256",
            "image_bytes",
            "build_id",
            "architecture",
            "runtime_interface",
            "config_id",
            "local_reference",
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
        raise RuntimeImagePreparationError(
            "runtime_image.identity_invalid", f"{field} is invalid"
        )
    return value


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
            "runtime_image.receipt_write_failed",
            "runtime image receipt could not be recorded",
        ) from error


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "FilesystemRuntimeImageStorage",
    "OCIImageTransport",
    "OciLayoutImageTransport",
    "PulledImageEvidence",
    "RuntimeArchitecture",
    "RuntimeImagePreparationError",
    "RuntimeImagePreparer",
    "RuntimeImageReceipt",
    "RuntimeImageReferenceIntent",
    "RuntimeImageStorage",
    "RuntimeInterface",
    "RuntimeInterfaceLabel",
    "prepare_runtime_image",
    "read_runtime_image_reference_intent",
    "record_runtime_image_reference",
    "runtime_image_expectations",
    "stored_runtime_image_resolver",
]
