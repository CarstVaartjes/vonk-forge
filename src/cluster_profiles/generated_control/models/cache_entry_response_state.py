from typing import Literal, cast

CacheEntryResponseState = Literal['cached', 'downloading', 'failed', 'incomplete', 'needs-repair', 'verifying']

CACHE_ENTRY_RESPONSE_STATE_VALUES: set[CacheEntryResponseState] = { 'cached', 'downloading', 'failed', 'incomplete', 'needs-repair', 'verifying',  }

def check_cache_entry_response_state(value: str) -> CacheEntryResponseState:
    if value in CACHE_ENTRY_RESPONSE_STATE_VALUES:
        return cast(CacheEntryResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_ENTRY_RESPONSE_STATE_VALUES!r}")
