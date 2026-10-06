from typing import Literal

ImageStoreCode = Literal['image_store.busy', 'image_store.collection_deferred', 'image_store.copy_failed', 'image_store.damaged_receipt_evicted', 'image_store.digest_invalid', 'image_store.import_incomplete', 'image_store.manifest_corrupt', 'image_store.manifest_invalid', 'image_store.manifest_unreadable', 'image_store.manifest_unsupported', 'image_store.reference_scan_failed', 'image_store.reference_unpinned', 'image_store.referenced_manifest_damaged']

IMAGE_STORE_CODE_VALUES: set[ImageStoreCode] = { 'image_store.busy', 'image_store.collection_deferred', 'image_store.copy_failed', 'image_store.damaged_receipt_evicted', 'image_store.digest_invalid', 'image_store.import_incomplete', 'image_store.manifest_corrupt', 'image_store.manifest_invalid', 'image_store.manifest_unreadable', 'image_store.manifest_unsupported', 'image_store.reference_scan_failed', 'image_store.reference_unpinned', 'image_store.referenced_manifest_damaged',  }

def check_image_store_code(value: str) -> ImageStoreCode:
    if value in IMAGE_STORE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {IMAGE_STORE_CODE_VALUES!r}")
