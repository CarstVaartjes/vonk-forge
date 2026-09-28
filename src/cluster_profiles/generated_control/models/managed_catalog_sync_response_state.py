from typing import Literal

ManagedCatalogSyncResponseState = Literal['current', 'failed', 'partial', 'syncing']

MANAGED_CATALOG_SYNC_RESPONSE_STATE_VALUES: set[ManagedCatalogSyncResponseState] = { 'current', 'failed', 'partial', 'syncing',  }

def check_managed_catalog_sync_response_state(value: str) -> ManagedCatalogSyncResponseState:
    if value in MANAGED_CATALOG_SYNC_RESPONSE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MANAGED_CATALOG_SYNC_RESPONSE_STATE_VALUES!r}")
