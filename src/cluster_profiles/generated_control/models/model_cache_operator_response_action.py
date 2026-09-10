from typing import Literal, cast

ModelCacheOperatorResponseAction = Literal['download', 'remove']

MODEL_CACHE_OPERATOR_RESPONSE_ACTION_VALUES: set[ModelCacheOperatorResponseAction] = { 'download', 'remove',  }

def check_model_cache_operator_response_action(value: str) -> ModelCacheOperatorResponseAction:
    if value in MODEL_CACHE_OPERATOR_RESPONSE_ACTION_VALUES:
        return cast(ModelCacheOperatorResponseAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATOR_RESPONSE_ACTION_VALUES!r}")
