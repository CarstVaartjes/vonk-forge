from typing import Literal, cast

CacheRemovalReviewResourceKind = Literal['model', 'recipe']

CACHE_REMOVAL_REVIEW_RESOURCE_KIND_VALUES: set[CacheRemovalReviewResourceKind] = { 'model', 'recipe',  }

def check_cache_removal_review_resource_kind(value: str) -> CacheRemovalReviewResourceKind:
    if value in CACHE_REMOVAL_REVIEW_RESOURCE_KIND_VALUES:
        return cast(CacheRemovalReviewResourceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REMOVAL_REVIEW_RESOURCE_KIND_VALUES!r}")
