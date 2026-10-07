from typing import Literal

ModelCacheCancellationObservationEffect = Literal['none', 'stopped', 'unknown']

MODEL_CACHE_CANCELLATION_OBSERVATION_EFFECT_VALUES: set[ModelCacheCancellationObservationEffect] = { 'none', 'stopped', 'unknown',  }

def check_model_cache_cancellation_observation_effect(value: str) -> ModelCacheCancellationObservationEffect:
    if value in MODEL_CACHE_CANCELLATION_OBSERVATION_EFFECT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_CANCELLATION_OBSERVATION_EFFECT_VALUES!r}")
