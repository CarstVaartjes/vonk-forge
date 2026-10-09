"""Images reason codes."""

from ..wire_model import WireEnum


class ImageStoreCode(WireEnum):
    """Refusals and damage found by the Controller OCI image store."""

    BUSY = "image_store.busy"
    COLLECTION_DEFERRED = "image_store.collection_deferred"
    COPY_FAILED = "image_store.copy_failed"
    DAMAGED_RECEIPT_EVICTED = "image_store.damaged_receipt_evicted"
    DIGEST_INVALID = "image_store.digest_invalid"
    IMPORT_INCOMPLETE = "image_store.import_incomplete"
    MANIFEST_CORRUPT = "image_store.manifest_corrupt"
    MANIFEST_INVALID = "image_store.manifest_invalid"
    MANIFEST_UNREADABLE = "image_store.manifest_unreadable"
    MANIFEST_UNSUPPORTED = "image_store.manifest_unsupported"
    REFERENCE_SCAN_FAILED = "image_store.reference_scan_failed"
    REFERENCE_UNPINNED = "image_store.reference_unpinned"
    REFERENCED_MANIFEST_DAMAGED = "image_store.referenced_manifest_damaged"


class PrebuiltImageCode(WireEnum):
    """Why a prebuilt runtime image is, or is not, used."""

    BUILD_KEY_MISMATCH = "prebuilt.build_key_mismatch"
    NOT_PINNED = "prebuilt.not_pinned"
    PULL_FAILED_RECENTLY = "prebuilt.pull_failed_recently"
    USED = "prebuilt.used"
