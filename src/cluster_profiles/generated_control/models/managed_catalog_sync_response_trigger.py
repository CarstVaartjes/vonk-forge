from typing import Literal, cast

ManagedCatalogSyncResponseTrigger = Literal['automatic', 'manual']

MANAGED_CATALOG_SYNC_RESPONSE_TRIGGER_VALUES: set[ManagedCatalogSyncResponseTrigger] = { 'automatic', 'manual',  }

def check_managed_catalog_sync_response_trigger(value: str) -> ManagedCatalogSyncResponseTrigger:
    if value in MANAGED_CATALOG_SYNC_RESPONSE_TRIGGER_VALUES:
        return cast(ManagedCatalogSyncResponseTrigger, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MANAGED_CATALOG_SYNC_RESPONSE_TRIGGER_VALUES!r}")
