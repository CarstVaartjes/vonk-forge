"""Content-addressed image storage and nonblocking publication fences."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from vonk_agent_protocol import (
    RuntimeImageCode,
    WaitReason,
)

from ..content_identity import ImageContent, differing_image_fields, same_image
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..oci_image_store import (
    DAMAGED_MANIFEST_CODES,
    IMAGE_CACHE_DIRECTORY,
    OciImageStore,
    OciImageStoreError,
    StoredImage,
    StoreUnknown,
)
from .contracts import (
    _IMAGE_DIGEST,
    _LOGGER,
    _MAX_RECEIPT_REJECTION_DETAIL,
    _SHA256,
    RuntimeImagePreparationError,
    RuntimeImagePreparationInvalid,
    RuntimeImagePreparationRefused,
    RuntimeImagePreparationUnknown,
    RuntimeImageReceipt,
    _atomic_json_replace,
    _load_receipt_document,
    _log_rejected_receipt,
    _ReceiptDocumentRejected,
    _runtime_interface_label,
    _same_image,
    _wire_architecture,
    _with_provenance,
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
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.IDENTITY_INVALID,
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
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.LOCK_UNAVAILABLE,
                "managed image publication lock directory is unavailable",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
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
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.LOCK_UNAVAILABLE,
                "managed image publication lock file is unavailable",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        os.close(directory_fd)
        with os.fdopen(descriptor, "a+b") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise RuntimeImagePreparationUnknown(
                    RuntimeImageCode.LOCK_UNAVAILABLE,
                    "managed image publication lock is not a regular file",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeImagePreparationUnknown(
                    RuntimeImageCode.PUBLICATION_CONTENDED,
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
            raise RuntimeImagePreparationRefused(
                RuntimeImageCode.ARCHIVE_MISMATCH,
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
                    raise RuntimeImagePreparationUnknown(
                        RuntimeImageCode.RECEIPT_CONTRACT_NEWER,
                        "runtime image receipt was written by a newer Controller contract",
                        retryable=True,
                        recovery_actions=("retry",),
                        reason=WaitReason.RECEIPT_MISSING,
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
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.CACHE_MISSING,
                "runtime image is not present in Controller storage",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if image.stored_bytes != expected_bytes:
            raise RuntimeImagePreparationRefused(
                RuntimeImageCode.ARCHIVE_MISMATCH,
                "stored runtime image does not match its recorded size",
            )
        return self.layout.blob_path(image.manifest_digest)

    def _stored_image(self, archive_sha256: str) -> StoredImage | None:
        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.ARCHIVE_INVALID, "runtime image address is invalid"
            )
        stored: StoredImage | StoreUnknown | None = None
        unreadable: str | None = None
        try:
            stored = self.layout.read(f"sha256:{archive_sha256}")
        except OciImageStoreError as error:
            if error.code not in DAMAGED_MANIFEST_CODES:
                unreadable = error.detail
            else:
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
        if isinstance(stored, StoreUnknown):
            unreadable = stored.detail
            stored = None
        if unreadable is not None:
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.ARCHIVE_UNAVAILABLE,
                unreadable,
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return stored

    def published_archive_bytes(self, archive_sha256: str) -> int:
        """The stored size of one image (shared layers included), or 0."""

        if _SHA256.fullmatch(archive_sha256) is None:
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.IDENTITY_INVALID,
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
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.IDENTITY_INVALID,
                "image removal requires an exact SHA-256",
            )
        try:
            (self.root / f"{archive_sha256}.receipt.json").unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeImagePreparationUnknown(
                RuntimeImageCode.REMOVAL_STORAGE_FAILED,
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
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.RECEIPT_INVALID,
                "source-build image evidence is invalid",
            )
        image = self._stored_image(archive_sha256)
        if image is None:
            return False
        if image.stored_bytes != expected_bytes:
            # The recorded size no longer describes the stored image: the recorded
            # build is not reusable, which is the answer to "is it available".
            # The caller builds again; the mismatch is recorded, not raised.
            retire_as_unknown(
                "runtime-image.archive",
                archive_sha256,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "stored runtime image size differs from the recorded size",
            )
            return False
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
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.DIGEST_INVALID, "runtime image identity is invalid"
            )
        expected = ImageContent(
            image_digest=image_digest,
            architecture=_wire_architecture(expected_architecture),
            runtime_interface=expected_runtime_interface,
            runtime_interface_label=_runtime_interface_label(
                expected_runtime_interface
            ),
        )
        for receipt in self._iter_receipts():
            if not same_image(receipt, expected):
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
            raise RuntimeImagePreparationInvalid(
                RuntimeImageCode.DIGEST_INVALID, "build input identity is invalid"
            )
        expected = ImageContent(
            architecture=_wire_architecture(expected_architecture),
            runtime_interface=expected_runtime_interface,
            runtime_interface_label=_runtime_interface_label(
                expected_runtime_interface
            ),
        )
        receipts = self._iter_receipts(expected_archive_sha256)
        for receipt in receipts:
            if receipt.build_input_sha256 != build_input_sha256:
                continue
            if differing_image_fields(receipt, expected):
                # Not a proof of the requested identity: a miss, so the
                # caller prepares the image again.
                continue
            if not self._archive_is_present(receipt):
                continue
            return receipt
        return None

    def _iter_receipts(
        self, archive_sha256: str | None = None
    ) -> Iterable[RuntimeImageReceipt]:
        """Yield every receipt the current contract can parse.

        A scan spans unrelated archives and recipes, so a file this contract
        cannot parse is one archive's stale metadata: skip it with a bounded
        warning naming its digest instead of failing every lookup that happens
        to walk past it.  ``read_receipt`` for that exact digest stays strict.
        Temporary read failures defer only their receipt while other verified
        candidates remain eligible. If none match, unresolved read uncertainty
        reaches the caller for observation and bounded retry, never as a miss
        that could trigger a needless rebuild or transfer.
        Access refusals remain immediate and are never treated as scan misses.
        """

        deferred: RuntimeImagePreparationUnknown | None = None
        for receipt_path in sorted(self.root.glob("*.receipt.json")):
            if archive_sha256 is not None and receipt_path.name != (
                f"{archive_sha256}.receipt.json"
            ):
                continue
            try:
                yield _load_receipt_document(receipt_path)
            except _ReceiptDocumentRejected as rejection:
                self._discard_rejected_receipt(receipt_path, rejection)
            except RuntimeImagePreparationUnknown as error:
                deferred = error
                if self._first_report(receipt_path):
                    _LOGGER.warning(
                        "deferred runtime image receipt %s: %s; %s",
                        receipt_path.name,
                        error.code,
                        error.detail,
                    )
        if deferred is not None:
            raise deferred

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
        except RuntimeImagePreparationUnknown:
            return False
        except RuntimeImagePreparationError as error:
            if error.code == RuntimeImageCode.CACHE_MISSING:
                return False
            if error.code == RuntimeImageCode.ARCHIVE_MISMATCH:
                # The receipt describes other bytes than the stored image: it
                # proves nothing about them, so a scan reads it as a miss and the
                # caller prepares the image again.
                retire_as_unknown(
                    "runtime-image.receipt",
                    receipt.oci_archive_sha256,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "receipt size differs from the stored image",
                )
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
            raise RuntimeImagePreparationUnknown(
                rejection.code, rejection.detail
            ) from rejection
