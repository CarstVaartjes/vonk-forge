from typing import Literal, cast

CacheRemovalAssetDisposition = Literal['remove', 'retain-shared']

CACHE_REMOVAL_ASSET_DISPOSITION_VALUES: set[CacheRemovalAssetDisposition] = { 'remove', 'retain-shared',  }

def check_cache_removal_asset_disposition(value: str) -> CacheRemovalAssetDisposition:
    if value in CACHE_REMOVAL_ASSET_DISPOSITION_VALUES:
        return cast(CacheRemovalAssetDisposition, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REMOVAL_ASSET_DISPOSITION_VALUES!r}")
