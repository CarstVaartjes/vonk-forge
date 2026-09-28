from typing import Literal

RecipeReadinessCheckState = Literal['blocked', 'ready', 'unavailable']

RECIPE_READINESS_CHECK_STATE_VALUES: set[RecipeReadinessCheckState] = { 'blocked', 'ready', 'unavailable',  }

def check_recipe_readiness_check_state(value: str) -> RecipeReadinessCheckState:
    if value in RECIPE_READINESS_CHECK_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_READINESS_CHECK_STATE_VALUES!r}")
