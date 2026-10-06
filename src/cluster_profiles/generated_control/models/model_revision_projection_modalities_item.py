from typing import Literal

ModelRevisionProjectionModalitiesItem = Literal['3d', 'audio', 'embeddings', 'image', 'text', 'video']

MODEL_REVISION_PROJECTION_MODALITIES_ITEM_VALUES: set[ModelRevisionProjectionModalitiesItem] = { '3d', 'audio', 'embeddings', 'image', 'text', 'video',  }

def check_model_revision_projection_modalities_item(value: str) -> ModelRevisionProjectionModalitiesItem:
    if value in MODEL_REVISION_PROJECTION_MODALITIES_ITEM_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_REVISION_PROJECTION_MODALITIES_ITEM_VALUES!r}")
