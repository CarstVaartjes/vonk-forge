from typing import Literal, cast

ModelCacheUpstreamRevisionStatus = Literal['check-failed', 'current', 'update-available']

MODEL_CACHE_UPSTREAM_REVISION_STATUS_VALUES: set[ModelCacheUpstreamRevisionStatus] = { 'check-failed', 'current', 'update-available',  }

def check_model_cache_upstream_revision_status(value: str) -> ModelCacheUpstreamRevisionStatus:
    if value in MODEL_CACHE_UPSTREAM_REVISION_STATUS_VALUES:
        return cast(ModelCacheUpstreamRevisionStatus, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_UPSTREAM_REVISION_STATUS_VALUES!r}")
