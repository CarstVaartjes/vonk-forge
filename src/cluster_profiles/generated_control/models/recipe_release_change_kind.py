from typing import Literal, cast

RecipeReleaseChangeKind = Literal['breaking', 'compatibility', 'fix', 'initial', 'metadata', 'model', 'performance', 'runtime', 'security']

RECIPE_RELEASE_CHANGE_KIND_VALUES: set[RecipeReleaseChangeKind] = { 'breaking', 'compatibility', 'fix', 'initial', 'metadata', 'model', 'performance', 'runtime', 'security',  }

def check_recipe_release_change_kind(value: str) -> RecipeReleaseChangeKind:
    if value in RECIPE_RELEASE_CHANGE_KIND_VALUES:
        return cast(RecipeReleaseChangeKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_RELEASE_CHANGE_KIND_VALUES!r}")
