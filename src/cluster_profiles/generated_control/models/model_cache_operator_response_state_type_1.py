from typing import Literal

ModelCacheOperatorResponseStateType1 = Literal['backoff', 'cancelled', 'failed', 'queued', 'running', 'succeeded']

MODEL_CACHE_OPERATOR_RESPONSE_STATE_TYPE_1_VALUES: set[ModelCacheOperatorResponseStateType1] = { 'backoff', 'cancelled', 'failed', 'queued', 'running', 'succeeded',  }

def check_model_cache_operator_response_state_type_1(value: str) -> ModelCacheOperatorResponseStateType1:
    if value in MODEL_CACHE_OPERATOR_RESPONSE_STATE_TYPE_1_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATOR_RESPONSE_STATE_TYPE_1_VALUES!r}")
