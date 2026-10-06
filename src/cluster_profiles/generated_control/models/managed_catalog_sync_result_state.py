from typing import Literal

ManagedCatalogSyncResultState = Literal['current', 'failed', 'partial']

MANAGED_CATALOG_SYNC_RESULT_STATE_VALUES: set[ManagedCatalogSyncResultState] = { 'current', 'failed', 'partial',  }

def check_managed_catalog_sync_result_state(value: str) -> ManagedCatalogSyncResultState:
    if value in MANAGED_CATALOG_SYNC_RESULT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MANAGED_CATALOG_SYNC_RESULT_STATE_VALUES!r}")
