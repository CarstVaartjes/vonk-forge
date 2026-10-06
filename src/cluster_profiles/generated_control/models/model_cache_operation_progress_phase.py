from typing import Literal

ModelCacheOperationProgressPhase = Literal['cancelling', 'completed', 'downloading', 'failed', 'queued', 'reclaiming', 'verifying']

MODEL_CACHE_OPERATION_PROGRESS_PHASE_VALUES: set[ModelCacheOperationProgressPhase] = { 'cancelling', 'completed', 'downloading', 'failed', 'queued', 'reclaiming', 'verifying',  }

def check_model_cache_operation_progress_phase(value: str) -> ModelCacheOperationProgressPhase:
    if value in MODEL_CACHE_OPERATION_PROGRESS_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_OPERATION_PROGRESS_PHASE_VALUES!r}")
