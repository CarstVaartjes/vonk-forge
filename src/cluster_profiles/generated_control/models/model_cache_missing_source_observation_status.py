from typing import Literal

ModelCacheMissingSourceObservationStatus = Literal[404, 410]

MODEL_CACHE_MISSING_SOURCE_OBSERVATION_STATUS_VALUES: set[ModelCacheMissingSourceObservationStatus] = { 404, 410,  }

def check_model_cache_missing_source_observation_status(value: int) -> ModelCacheMissingSourceObservationStatus:
    if value in MODEL_CACHE_MISSING_SOURCE_OBSERVATION_STATUS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_MISSING_SOURCE_OBSERVATION_STATUS_VALUES!r}")
