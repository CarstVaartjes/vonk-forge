from typing import Literal

AssetAvailability = Literal['missing', 'partial', 'unknown', 'verified']

ASSET_AVAILABILITY_VALUES: set[AssetAvailability] = { 'missing', 'partial', 'unknown', 'verified',  }

def check_asset_availability(value: str) -> AssetAvailability:
    if value in ASSET_AVAILABILITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ASSET_AVAILABILITY_VALUES!r}")
