from typing import Literal

RecipeBuildOptionsFormat = Literal['docker', 'oci']

RECIPE_BUILD_OPTIONS_FORMAT_VALUES: set[RecipeBuildOptionsFormat] = { 'docker', 'oci',  }

def check_recipe_build_options_format(value: str) -> RecipeBuildOptionsFormat:
    if value in RECIPE_BUILD_OPTIONS_FORMAT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_OPTIONS_FORMAT_VALUES!r}")
