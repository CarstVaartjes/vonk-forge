from typing import Literal

ModelCacheOperatorResponseStateType0 = Literal['accepted', 'cancelling']

MODEL_CACHE_OPERATOR_RESPONSE_STATE_TYPE_0_VALUES: set[ModelCacheOperatorResponseStateType0] = { 'accepted', 'cancelling',  }

def check_model_cache_operator_response_state_type_0(value: str) -> ModelCacheOperatorResponseStateType0:
    if value in MODEL_CACHE_OPERATOR_RESPONSE_STATE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATOR_RESPONSE_STATE_TYPE_0_VALUES!r}")
