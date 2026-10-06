from typing import Literal

ModelCacheBlockerCode = Literal['insufficient-reserved-storage', 'model-not-cached', 'recipe-not-cached']

MODEL_CACHE_BLOCKER_CODE_VALUES: set[ModelCacheBlockerCode] = { 'insufficient-reserved-storage', 'model-not-cached', 'recipe-not-cached',  }

def check_model_cache_blocker_code(value: str) -> ModelCacheBlockerCode:
    if value in MODEL_CACHE_BLOCKER_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_BLOCKER_CODE_VALUES!r}")
