from typing import Literal, cast

ModelDefinitionModalitiesItem = Literal['3d', 'audio', 'embeddings', 'image', 'text', 'video']

MODEL_DEFINITION_MODALITIES_ITEM_VALUES: set[ModelDefinitionModalitiesItem] = { '3d', 'audio', 'embeddings', 'image', 'text', 'video',  }

def check_model_definition_modalities_item(value: str) -> ModelDefinitionModalitiesItem:
    if value in MODEL_DEFINITION_MODALITIES_ITEM_VALUES:
        return cast(ModelDefinitionModalitiesItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_DEFINITION_MODALITIES_ITEM_VALUES!r}")
