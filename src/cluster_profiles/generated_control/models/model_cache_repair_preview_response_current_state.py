from typing import Literal, cast

ModelCacheRepairPreviewResponseCurrentState = Literal['cached', 'downloading', 'failed', 'incomplete', 'needs-repair', 'verifying']

MODEL_CACHE_REPAIR_PREVIEW_RESPONSE_CURRENT_STATE_VALUES: set[ModelCacheRepairPreviewResponseCurrentState] = { 'cached', 'downloading', 'failed', 'incomplete', 'needs-repair', 'verifying',  }

def check_model_cache_repair_preview_response_current_state(value: str) -> ModelCacheRepairPreviewResponseCurrentState:
    if value in MODEL_CACHE_REPAIR_PREVIEW_RESPONSE_CURRENT_STATE_VALUES:
        return cast(ModelCacheRepairPreviewResponseCurrentState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CACHE_REPAIR_PREVIEW_RESPONSE_CURRENT_STATE_VALUES!r}")
