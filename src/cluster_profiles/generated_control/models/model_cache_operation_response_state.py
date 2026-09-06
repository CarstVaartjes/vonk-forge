from typing import Literal, cast

ModelCacheOperationResponseState = Literal['cancelled', 'failed', 'partial', 'queued', 'running', 'succeeded']

MODEL_CACHE_OPERATION_RESPONSE_STATE_VALUES: set[ModelCacheOperationResponseState] = { 'cancelled', 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_model_cache_operation_response_state(value: str) -> ModelCacheOperationResponseState:
    if value in MODEL_CACHE_OPERATION_RESPONSE_STATE_VALUES:
        return cast(ModelCacheOperationResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATION_RESPONSE_STATE_VALUES!r}")
