from typing import Literal

ModelCacheOperatorStatus = Literal['accepted']

MODEL_CACHE_OPERATOR_STATUS_VALUES: set[ModelCacheOperatorStatus] = { 'accepted',  }

def check_model_cache_operator_status(value: str) -> ModelCacheOperatorStatus:
    if value in MODEL_CACHE_OPERATOR_STATUS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATOR_STATUS_VALUES!r}")
