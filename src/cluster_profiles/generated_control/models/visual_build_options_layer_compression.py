from typing import Literal, cast

VisualBuildOptionsLayerCompression = Literal['disabled', 'gzip']

VISUAL_BUILD_OPTIONS_LAYER_COMPRESSION_VALUES: set[VisualBuildOptionsLayerCompression] = { 'disabled', 'gzip',  }

def check_visual_build_options_layer_compression(value: str) -> VisualBuildOptionsLayerCompression:
    if value in VISUAL_BUILD_OPTIONS_LAYER_COMPRESSION_VALUES:
        return cast(VisualBuildOptionsLayerCompression, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_BUILD_OPTIONS_LAYER_COMPRESSION_VALUES!r}")
