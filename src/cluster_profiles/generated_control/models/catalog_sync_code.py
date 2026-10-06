from typing import Literal

CatalogSyncCode = Literal['catalog.sync_actor_invalid', 'catalog.sync_commit_invalid', 'catalog.sync_failed', 'catalog.sync_identity_changed', 'catalog.sync_in_progress', 'catalog.sync_item_failed', 'catalog.sync_lease_expired', 'catalog.sync_model_failed', 'catalog.sync_not_found', 'catalog.sync_prebuilt_images_failed', 'catalog.sync_preview_changed', 'catalog.sync_repository_changed', 'catalog.sync_request_invalid', 'catalog.sync_request_reused', 'catalog.sync_result_unreadable', 'catalog.sync_revision_changed', 'catalog.sync_state_invalid', 'catalog.sync_trigger_invalid', 'recipe.topology_changed']

CATALOG_SYNC_CODE_VALUES: set[CatalogSyncCode] = { 'catalog.sync_actor_invalid', 'catalog.sync_commit_invalid', 'catalog.sync_failed', 'catalog.sync_identity_changed', 'catalog.sync_in_progress', 'catalog.sync_item_failed', 'catalog.sync_lease_expired', 'catalog.sync_model_failed', 'catalog.sync_not_found', 'catalog.sync_prebuilt_images_failed', 'catalog.sync_preview_changed', 'catalog.sync_repository_changed', 'catalog.sync_request_invalid', 'catalog.sync_request_reused', 'catalog.sync_result_unreadable', 'catalog.sync_revision_changed', 'catalog.sync_state_invalid', 'catalog.sync_trigger_invalid', 'recipe.topology_changed',  }

def check_catalog_sync_code(value: str) -> CatalogSyncCode:
    if value in CATALOG_SYNC_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CATALOG_SYNC_CODE_VALUES!r}")
