from typing import Literal, cast

RecipeUpdateResponseState = Literal['cancelled', 'cancelling', 'failed', 'partial', 'queued', 'running', 'succeeded']

RECIPE_UPDATE_RESPONSE_STATE_VALUES: set[RecipeUpdateResponseState] = { 'cancelled', 'cancelling', 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_recipe_update_response_state(value: str) -> RecipeUpdateResponseState:
    if value in RECIPE_UPDATE_RESPONSE_STATE_VALUES:
        return cast(RecipeUpdateResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_UPDATE_RESPONSE_STATE_VALUES!r}")
