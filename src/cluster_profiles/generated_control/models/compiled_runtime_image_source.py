from typing import Literal, cast

CompiledRuntimeImageSource = Literal['controller-build', 'published']

COMPILED_RUNTIME_IMAGE_SOURCE_VALUES: set[CompiledRuntimeImageSource] = { 'controller-build', 'published',  }

def check_compiled_runtime_image_source(value: str) -> CompiledRuntimeImageSource:
    if value in COMPILED_RUNTIME_IMAGE_SOURCE_VALUES:
        return cast(CompiledRuntimeImageSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_RUNTIME_IMAGE_SOURCE_VALUES!r}")
