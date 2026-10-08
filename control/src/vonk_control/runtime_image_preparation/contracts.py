"""Runtime image receipt contracts and managed metadata I/O."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, ValidationError
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    RuntimeImageCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.wire_model import Digest, WireModel

from ..categorized_faults import security_reason
from ..integer_domains import MAX_DATABASE_INTEGER
from ..validation_detail import validation_error_detail

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


_LOGGER = logging.getLogger(__name__)


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


class RuntimeImagePreparationRefused(
    SecurityRefusalError, RuntimeImagePreparationError
):
    """A refusal at a security boundary: an archive, digest, architecture or interface that does not match the authorized image."""

    def __init__(
        self,
        *args: Any,
        reason: SecurityRefusalReason | None = None,
        **fields: Any,
    ) -> None:
        RuntimeImagePreparationError.__init__(self, *args, **fields)
        self.typed_reason = (
            reason if reason is not None else security_reason(args[0] if args else None)
        )


class RuntimeImagePreparationInvalid(InvalidRequestError, RuntimeImagePreparationError):
    """An identity, receipt, recipe or evidence document that is malformed or out of contract."""

    def __init__(
        self,
        *args: Any,
        reason: InvalidRequestReason | None = InvalidRequestReason.MALFORMED,
        field: str | None = None,
        **fields: Any,
    ) -> None:
        RuntimeImagePreparationError.__init__(self, *args, **fields)
        self.typed_reason = reason
        self.typed_field = field


class RuntimeImagePreparationUnknown(UnknownOutcomeError, RuntimeImagePreparationError):
    """Unconfirmed storage or bookkeeping: the owner observes it again and retries; never a refusal."""

    def __init__(
        self,
        *args: Any,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
        **fields: Any,
    ) -> None:
        RuntimeImagePreparationError.__init__(self, *args, **fields)
        self.typed_reason = reason


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


ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


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
    attempt: int = Field(le=MAX_DATABASE_INTEGER, strict=True, ge=1)
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
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.REFERENCE_INTENT_INVALID,
            "runtime image reference intent is malformed",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def _parse_runtime_image_receipt(value: object) -> RuntimeImageReceipt:
    if not isinstance(value, Mapping) or value.get("schema_version") != 2:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_INVALID,
            "runtime image receipt schema version is unsupported",
            reason=WaitReason.RECEIPT_MISSING,
        )
    try:
        return RuntimeImageReceipt.model_validate(value)
    except (TypeError, ValueError) as error:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_UNAVAILABLE,
            "runtime image receipt identity is unavailable or malformed"
            + validation_error_detail(error),
            reason=WaitReason.RECEIPT_MISSING,
        ) from error


class _ReceiptDocumentRejected(UnknownOutcomeError):
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
        super().__init__(detail, reason=WaitReason.RECEIPT_MISSING)


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

    A denied read raises an explicit security refusal. Other unreadable files
    raise retryable evidence uncertainty; scans can inspect independent files,
    but cannot turn unresolved uncertainty into an absent-image verdict.
    A document that is present but does not
    satisfy the current contract raises ``_ReceiptDocumentRejected`` naming the
    rule that rejected it.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError as error:
        raise RuntimeImagePreparationRefused(
            SecurityRefusalReason.PERMISSION_DENIED,
            "access to the managed runtime image receipt was denied",
            reason=SecurityRefusalReason.PERMISSION_DENIED,
        ) from error
    except OSError as error:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_UNAVAILABLE,
            "runtime image receipt could not be read",
            retryable=True,
            reason=WaitReason.RECEIPT_MISSING,
        ) from error
    except UnicodeDecodeError as error:
        raise _ReceiptDocumentRejected(
            RuntimeImageCode.RECEIPT_UNAVAILABLE,
            "runtime image receipt is not valid UTF-8 JSON",
        ) from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise _ReceiptDocumentRejected(
            RuntimeImageCode.RECEIPT_UNAVAILABLE,
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
            RuntimeImageCode.RECEIPT_UNAVAILABLE,
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
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_WRITE_FAILED,
            "runtime image receipt could not be recorded",
            retryable=True,
            recovery_actions=("retry",),
            reason=WaitReason.RECEIPT_MISSING,
        ) from error


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
