from typing import Literal, cast

CacheRemovalFindingAssetKind = Literal['model-object', 'model-set', 'runtime-image']

CACHE_REMOVAL_FINDING_ASSET_KIND_VALUES: set[CacheRemovalFindingAssetKind] = { 'model-object', 'model-set', 'runtime-image',  }

def check_cache_removal_finding_asset_kind(value: str) -> CacheRemovalFindingAssetKind:
    if value in CACHE_REMOVAL_FINDING_ASSET_KIND_VALUES:
        return cast(CacheRemovalFindingAssetKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REMOVAL_FINDING_ASSET_KIND_VALUES!r}")
