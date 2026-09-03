from typing import Literal, cast

VisualBuildOptionsFormat = Literal['docker', 'oci']

VISUAL_BUILD_OPTIONS_FORMAT_VALUES: set[VisualBuildOptionsFormat] = { 'docker', 'oci',  }

def check_visual_build_options_format(value: str) -> VisualBuildOptionsFormat:
    if value in VISUAL_BUILD_OPTIONS_FORMAT_VALUES:
        return cast(VisualBuildOptionsFormat, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_BUILD_OPTIONS_FORMAT_VALUES!r}")
