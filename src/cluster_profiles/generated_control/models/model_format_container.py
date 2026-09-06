from typing import Literal, cast

ModelFormatContainer = Literal['gguf', 'onnx', 'other', 'safetensors']

MODEL_FORMAT_CONTAINER_VALUES: set[ModelFormatContainer] = { 'gguf', 'onnx', 'other', 'safetensors',  }

def check_model_format_container(value: str) -> ModelFormatContainer:
    if value in MODEL_FORMAT_CONTAINER_VALUES:
        return cast(ModelFormatContainer, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_FORMAT_CONTAINER_VALUES!r}")
