from typing import Literal

CatalogSyncState = Literal['current', 'failed', 'partial', 'syncing']

CATALOG_SYNC_STATE_VALUES: set[CatalogSyncState] = { 'current', 'failed', 'partial', 'syncing',  }

def check_catalog_sync_state(value: str) -> CatalogSyncState:
    if value in CATALOG_SYNC_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CATALOG_SYNC_STATE_VALUES!r}")
