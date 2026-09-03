from typing import Literal, cast

VisualBuildOptionsSquash = Literal['all', 'new', 'none']

VISUAL_BUILD_OPTIONS_SQUASH_VALUES: set[VisualBuildOptionsSquash] = { 'all', 'new', 'none',  }

def check_visual_build_options_squash(value: str) -> VisualBuildOptionsSquash:
    if value in VISUAL_BUILD_OPTIONS_SQUASH_VALUES:
        return cast(VisualBuildOptionsSquash, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_BUILD_OPTIONS_SQUASH_VALUES!r}")
