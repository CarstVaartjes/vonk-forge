from typing import Literal

CacheReferenceReason = Literal['recipe-installation', 'running-model', 'saved-profile']

CACHE_REFERENCE_REASON_VALUES: set[CacheReferenceReason] = { 'recipe-installation', 'running-model', 'saved-profile',  }

def check_cache_reference_reason(value: str) -> CacheReferenceReason:
    if value in CACHE_REFERENCE_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_REFERENCE_REASON_VALUES!r}")
