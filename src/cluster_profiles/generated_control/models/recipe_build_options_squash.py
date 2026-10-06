from typing import Literal

RecipeBuildOptionsSquash = Literal['all', 'new', 'none']

RECIPE_BUILD_OPTIONS_SQUASH_VALUES: set[RecipeBuildOptionsSquash] = { 'all', 'new', 'none',  }

def check_recipe_build_options_squash(value: str) -> RecipeBuildOptionsSquash:
    if value in RECIPE_BUILD_OPTIONS_SQUASH_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_OPTIONS_SQUASH_VALUES!r}")
