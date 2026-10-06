from typing import Literal

StorageDemandCode = Literal['storage.evicting', 'storage.insufficient_after_eviction']

STORAGE_DEMAND_CODE_VALUES: set[StorageDemandCode] = { 'storage.evicting', 'storage.insufficient_after_eviction',  }

def check_storage_demand_code(value: str) -> StorageDemandCode:
    if value in STORAGE_DEMAND_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STORAGE_DEMAND_CODE_VALUES!r}")
