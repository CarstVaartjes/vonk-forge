from typing import Literal, cast

RecipeRevisionResponseOrigin = Literal['global', 'local', 'recipe_library', 'workload_run']

RECIPE_REVISION_RESPONSE_ORIGIN_VALUES: set[RecipeRevisionResponseOrigin] = { 'global', 'local', 'recipe_library', 'workload_run',  }

def check_recipe_revision_response_origin(value: str) -> RecipeRevisionResponseOrigin:
    if value in RECIPE_REVISION_RESPONSE_ORIGIN_VALUES:
        return cast(RecipeRevisionResponseOrigin, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_REVISION_RESPONSE_ORIGIN_VALUES!r}")
