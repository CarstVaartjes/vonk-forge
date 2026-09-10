from typing import Literal, cast

ModelCacheOperatorResponseState = Literal['accepted', 'cancelled', 'failed', 'partial', 'queued', 'running', 'succeeded']

MODEL_CACHE_OPERATOR_RESPONSE_STATE_VALUES: set[ModelCacheOperatorResponseState] = { 'accepted', 'cancelled', 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_model_cache_operator_response_state(value: str) -> ModelCacheOperatorResponseState:
    if value in MODEL_CACHE_OPERATOR_RESPONSE_STATE_VALUES:
        return cast(ModelCacheOperatorResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATOR_RESPONSE_STATE_VALUES!r}")
