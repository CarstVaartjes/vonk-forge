from typing import Literal

RecipeAlternativeCache = Literal['cached', 'failed', 'not_cached', 'preparing', 'unknown']

RECIPE_ALTERNATIVE_CACHE_VALUES: set[RecipeAlternativeCache] = { 'cached', 'failed', 'not_cached', 'preparing', 'unknown',  }

def check_recipe_alternative_cache(value: str) -> RecipeAlternativeCache:
    if value in RECIPE_ALTERNATIVE_CACHE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_ALTERNATIVE_CACHE_VALUES!r}")
