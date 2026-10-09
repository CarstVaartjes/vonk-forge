"""Request-led preparation from verified build evidence."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from vonk_agent_protocol import (
    LifecycleState,
    RuntimeImageCode,
    WaitReason,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256, read_recipe

from ..content_identity import ImageContent, same_image
from ..models import RecipeBuild
from ..operation_contract import AvailabilityRecoveryAction
from ..runtime_adapters import (
    RuntimeAdapter,
    resolve_runtime_adapter,
)
from ..runtime_spec_contract import RuntimeSpec
from .contracts import (
    _IMAGE_DIGEST,
    _RUNTIME_ARCHITECTURE,
    _RUNTIME_INTERFACE,
    _RUNTIME_INTERFACE_LABEL,
    _SHA256,
    OCIImageTransport,
    RuntimeArchitecture,
    RuntimeImagePreparationRefused,
    RuntimeImagePreparationUnknown,
    RuntimeImageReceipt,
    RuntimeImageStorage,
    RuntimeInterface,
    RuntimeInterfaceLabel,
    _runtime_interface_label,
    _wire_architecture,
    _with_provenance,
)
from .transport import OciLayoutImageTransport, _validate_evidence


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
    """Observe preparation with a finite, resource-free retry budget.

    Only observations retry. Authorization denials escape immediately, and
    each attempt releases its publication fence before the bounded backoff.
    The durable requesting owner retains its own absolute operation deadline.
    """

    def prepare_once() -> RuntimeImageReceipt:
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
            except RuntimeImagePreparationRefused:
                raise
            except RuntimeImagePreparationUnknown:
                raise
            except Exception as error:
                raise RuntimeImagePreparationUnknown(
                    RuntimeImageCode.RECEIPT_PERSISTENCE_FAILED,
                    "durable runtime image receipt could not be persisted",
                    retryable=True,
                    recovery_actions=(AvailabilityRecoveryAction.RETRY,),
                    reason=WaitReason.RECEIPT_MISSING,
                ) from error
        return receipt

    last_error: RuntimeImagePreparationUnknown | None = None
    for delay in (0.0, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        try:
            return prepare_once()
        except RuntimeImagePreparationUnknown as error:
            last_error = error
    assert last_error is not None
    raise last_error


class RuntimeImagePreparer(Protocol):
    def __call__(
        self,
        document: Mapping[str, object],
        runtime_spec: RuntimeSpec,
        build: RecipeBuild | None,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ) -> RuntimeImageReceipt:
        """Prepare a verified receipt, with optional owner fencing."""
        ...


def make_runtime_image_receipt_preparer(
    storage: RuntimeImageStorage,
    transport: OCIImageTransport,
    *,
    clock: Callable[[], datetime],
) -> RuntimeImagePreparer:
    """Build the production callback that prepares an image.

    API and worker composition share this callback so recovery and build
    execution prepare an image the same way.
    """

    def prepare(
        document: Mapping[str, object],
        runtime_spec: RuntimeSpec,
        build: RecipeBuild | None,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ) -> RuntimeImageReceipt:
        if build is None:
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.BUILD_INCOMPLETE,
                "source build receipt is unavailable",
                retryable=True,
                reason=WaitReason.RECEIPT_MISSING,
            )
        build_receipt = {
            "state": build.state,
            "build_id": build.id,
            "build_input_sha256": build.build_input_sha256,
            "image_digest": build.image_digest,
            "oci_layout_sha256": build.oci_layout_sha256,
            "image_bytes": build.image_bytes,
        }

        return prepare_runtime_image(
            document,
            runtime=runtime_spec.runtime,
            storage=storage,
            transport=transport,
            build_receipt=build_receipt,
            now=clock(),
            before_publish=before_publish,
        )

    return prepare


def stored_runtime_image_resolver(
    storage: RuntimeImageStorage,
) -> Callable[[object, str, RuntimeSpec], RuntimeImageReceipt | None]:
    """Resolve a compiled image to the verified archive managed storage holds.

    The image is identified by its content: no recipe revision is consulted.
    A missing archive is a wait for the ordinary preparation phase, never a
    refusal.
    """

    def resolve(
        _document: object, image_digest: str, runtime_spec: RuntimeSpec
    ) -> RuntimeImageReceipt | None:
        expectations = runtime_image_expectations(runtime_spec.runtime)
        receipt = storage.find_verified(
            image_digest,
            expected_architecture=expectations["architecture"],
            expected_runtime_interface=expectations["interface"],
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
    if value.get("state") != LifecycleState.SUCCEEDED.value:
        # Not damage and not a recipe fault: the build has not finished (or its
        # row was read mid-update). The owner observes the build again.
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.BUILD_INCOMPLETE,
            "source-build receipt is not succeeded",
            retryable=True,
            recovery_actions=(AvailabilityRecoveryAction.RETRY,),
            reason=WaitReason.RECEIPT_MISSING,
        )
    archive_sha = value.get("oci_layout_sha256")
    if not isinstance(archive_sha, str) or _SHA256.fullmatch(archive_sha) is None:
        # Without the archive identity nothing can be located or re-derived.
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_INVALID,
            "source-build receipt names no valid image archive",
            retryable=True,
            recovery_actions=(AvailabilityRecoveryAction.RETRY,),
            reason=WaitReason.RECEIPT_MISSING,
        )
    try:
        cached = storage.read_receipt(archive_sha)
    except RuntimeImagePreparationUnknown:
        # Rebuild absent or invalid metadata from the verified archive and
        # current build evidence. No alternate receipt shape is accepted.
        cached = None
    # The build row's image evidence can be damaged in part (a lost or invalid
    # field). The receipt the image cache already holds for this exact archive
    # is evidence of the same image; each damaged field is re-derived from it,
    # and only a field that no evidence supplies leaves the receipt unconfirmed
    # (the build is then observed and, if need be, run again).
    image_digest = value.get("image_digest")
    if (
        not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
    ):
        image_digest = None if cached is None else cached.image_digest
    build_id = value.get("build_id")
    if not isinstance(build_id, str) or not build_id:
        build_id = None if cached is None else cached.build_id
    image_bytes = value.get("image_bytes")
    # Size bookkeeping is reconstructed from the exact managed image. A valid
    # integer in a damaged build row is still bookkeeping, not byte authority.
    stored_bytes = storage.published_archive_bytes(archive_sha)
    if stored_bytes > 0:
        image_bytes = stored_bytes
    elif type(image_bytes) is not int or image_bytes < 1:
        image_bytes = None if cached is None else cached.image_bytes
    if image_digest is None or build_id is None or image_bytes is None:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_INVALID,
            "source-build image evidence is damaged and cannot be re-derived",
            retryable=True,
            recovery_actions=(AvailabilityRecoveryAction.RETRY,),
            reason=WaitReason.RECEIPT_MISSING,
        )
    raw_build_input = value.get("build_input_sha256")
    if raw_build_input is not None and (
        not isinstance(raw_build_input, str)
        or _SHA256.fullmatch(raw_build_input) is None
    ):
        # Reuse provenance only: the cached receipt's own identity (below) or
        # none, never a reason to refuse an image whose archive verifies.
        raw_build_input = None
    existing = storage.existing_archive(archive_sha, image_bytes)
    expected_interface_label = _runtime_interface_label(expected_interface)
    observed = ImageContent(
        image_digest=image_digest,
        archive_sha256=archive_sha,
        image_bytes=image_bytes,
        architecture=_wire_architecture(expected_architecture),
        runtime_interface=expected_interface,
        runtime_interface_label=expected_interface_label,
    )
    if cached is not None and same_image(cached, observed):
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
    try:
        observed = transport.inspect_archive(
            existing,
            expected_architecture=expected_architecture,
            expected_runtime_interface=expected_interface_label,
            expected_archive_sha256=archive_sha,
            expected_archive_bytes=image_bytes,
        )
    except RuntimeImagePreparationRefused:
        raise
    except RuntimeImagePreparationUnknown:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.INSPECT_INVALID,
            "image transport observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    _validate_evidence(observed, expected_architecture, expected_interface_label)
    if observed.archive_sha256 != archive_sha or observed.archive_bytes != image_bytes:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.EVIDENCE_INVALID,
            "transport observation does not identify the exact stored archive",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    architecture, runtime_interface, runtime_interface_label = (
        _receipt_runtime_identity(expected_architecture, expected_interface)
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
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECIPE_INVALID,
            "canonical RecipeDefinition document is unavailable",
        )
    try:
        return read_recipe(raw)
    except Exception as error:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECIPE_INVALID,
            "recipe does not satisfy canonical RecipeDefinition",
        ) from error


def runtime_image_expectations(value: Mapping[str, object] | object) -> dict[str, str]:
    """Read image verification expectations from the current compiler projection."""
    dump = getattr(value, "model_dump", None)
    raw: object = dump(mode="json") if callable(dump) else value
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RUNTIME_INVALID,
            "canonical runtime projection is unavailable",
        )
    if "runtime_interface" in raw:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RUNTIME_INVALID,
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
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RUNTIME_INVALID,
            "runtime projection lacks observed architecture/interface expectations",
        )
    return {"architecture": architecture, "interface": interface}


def _receipt_runtime_identity(
    expected_architecture: str, expected_interface: str
) -> tuple[RuntimeArchitecture, RuntimeInterface, RuntimeInterfaceLabel]:
    """Project the accepted compiler decision into the current receipt contract.

    Missing/stale compiler evidence is re-observed; image labels never make a
    second compatibility decision.
    """
    if (
        _wire_architecture(expected_architecture) != _RUNTIME_ARCHITECTURE
        or expected_interface != _RUNTIME_INTERFACE
    ):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RUNTIME_INVALID,
            "accepted runtime projection is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return _RUNTIME_ARCHITECTURE, _RUNTIME_INTERFACE, _RUNTIME_INTERFACE_LABEL


def _recipe_digest(value: object) -> str:
    """Return the stored digest of a published recipe document."""

    digest = getattr(value, "content_digest", None)
    if isinstance(digest, str):
        return digest
    raw = getattr(value, "document", value)
    if not isinstance(raw, Mapping):
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECIPE_INVALID,
            "published recipe document is unavailable",
        )
    return document_sha256(raw)


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
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.RECEIPT_INVALID,
            "source-build receipt is not readable",
            retryable=True,
            reason=WaitReason.RECEIPT_MISSING,
        )
    return data


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeImagePreparationUnknown(
            RuntimeImageCode.IDENTITY_INVALID, f"{field} is invalid"
        )
    return value


def _timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
