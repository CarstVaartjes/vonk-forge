from typing import Literal, cast

ModelCacheOperationResponseKind = Literal['download', 'evict', 'repair']

MODEL_CACHE_OPERATION_RESPONSE_KIND_VALUES: set[ModelCacheOperationResponseKind] = { 'download', 'evict', 'repair',  }

def check_model_cache_operation_response_kind(value: str) -> ModelCacheOperationResponseKind:
    if value in MODEL_CACHE_OPERATION_RESPONSE_KIND_VALUES:
        return cast(ModelCacheOperationResponseKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATION_RESPONSE_KIND_VALUES!r}")
