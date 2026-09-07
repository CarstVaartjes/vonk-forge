from typing import Literal, cast

CacheArtifactResponseState = Literal['corrupt', 'missing', 'partial', 'verified']

CACHE_ARTIFACT_RESPONSE_STATE_VALUES: set[CacheArtifactResponseState] = { 'corrupt', 'missing', 'partial', 'verified',  }

def check_cache_artifact_response_state(value: str) -> CacheArtifactResponseState:
    if value in CACHE_ARTIFACT_RESPONSE_STATE_VALUES:
        return cast(CacheArtifactResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CACHE_ARTIFACT_RESPONSE_STATE_VALUES!r}")
