from typing import Literal

RecipeBuildOptionsLayerCompression = Literal['disabled', 'gzip']

RECIPE_BUILD_OPTIONS_LAYER_COMPRESSION_VALUES: set[RecipeBuildOptionsLayerCompression] = { 'disabled', 'gzip',  }

def check_recipe_build_options_layer_compression(value: str) -> RecipeBuildOptionsLayerCompression:
    if value in RECIPE_BUILD_OPTIONS_LAYER_COMPRESSION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_OPTIONS_LAYER_COMPRESSION_VALUES!r}")
