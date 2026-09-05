from typing import Literal, cast

LibraryModelFormatContainer = Literal['gguf', 'safetensors']

LIBRARY_MODEL_FORMAT_CONTAINER_VALUES: set[LibraryModelFormatContainer] = { 'gguf', 'safetensors',  }

def check_library_model_format_container(value: str) -> LibraryModelFormatContainer:
    if value in LIBRARY_MODEL_FORMAT_CONTAINER_VALUES:
        return cast(LibraryModelFormatContainer, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_MODEL_FORMAT_CONTAINER_VALUES!r}")
