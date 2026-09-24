from typing import Literal, cast

RecipeUpdateChildState = Literal['cancelled', 'cancelling', 'failed', 'partial', 'pending', 'queued', 'running', 'succeeded']

RECIPE_UPDATE_CHILD_STATE_VALUES: set[RecipeUpdateChildState] = { 'cancelled', 'cancelling', 'failed', 'partial', 'pending', 'queued', 'running', 'succeeded',  }

def check_recipe_update_child_state(value: str) -> RecipeUpdateChildState:
    if value in RECIPE_UPDATE_CHILD_STATE_VALUES:
        return cast(RecipeUpdateChildState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_UPDATE_CHILD_STATE_VALUES!r}")
