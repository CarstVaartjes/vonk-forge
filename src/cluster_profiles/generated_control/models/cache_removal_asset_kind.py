from typing import Literal

CacheRemovalAssetKind = Literal['model-object', 'model-set', 'runtime-image']

CACHE_REMOVAL_ASSET_KIND_VALUES: set[CacheRemovalAssetKind] = { 'model-object', 'model-set', 'runtime-image',  }

def check_cache_removal_asset_kind(value: str) -> CacheRemovalAssetKind:
    if value in CACHE_REMOVAL_ASSET_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REMOVAL_ASSET_KIND_VALUES!r}")
