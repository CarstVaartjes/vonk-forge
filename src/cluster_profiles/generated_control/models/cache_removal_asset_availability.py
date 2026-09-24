from typing import Literal, cast

CacheRemovalAssetAvailability = Literal['missing', 'partial', 'unknown', 'verified']

CACHE_REMOVAL_ASSET_AVAILABILITY_VALUES: set[CacheRemovalAssetAvailability] = { 'missing', 'partial', 'unknown', 'verified',  }

def check_cache_removal_asset_availability(value: str) -> CacheRemovalAssetAvailability:
    if value in CACHE_REMOVAL_ASSET_AVAILABILITY_VALUES:
        return cast(CacheRemovalAssetAvailability, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REMOVAL_ASSET_AVAILABILITY_VALUES!r}")
