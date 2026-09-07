from typing import Literal, cast

CacheEntryResponseCoverage = Literal['complete', 'incomplete']

CACHE_ENTRY_RESPONSE_COVERAGE_VALUES: set[CacheEntryResponseCoverage] = { 'complete', 'incomplete',  }

def check_cache_entry_response_coverage(value: str) -> CacheEntryResponseCoverage:
    if value in CACHE_ENTRY_RESPONSE_COVERAGE_VALUES:
        return cast(CacheEntryResponseCoverage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_ENTRY_RESPONSE_COVERAGE_VALUES!r}")
