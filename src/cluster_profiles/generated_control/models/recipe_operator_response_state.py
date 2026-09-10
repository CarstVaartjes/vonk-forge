from typing import Literal, cast

RecipeOperatorResponseState = Literal['accepted', 'cancelled', 'failed', 'partial', 'queued', 'running', 'succeeded']

RECIPE_OPERATOR_RESPONSE_STATE_VALUES: set[RecipeOperatorResponseState] = { 'accepted', 'cancelled', 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_recipe_operator_response_state(value: str) -> RecipeOperatorResponseState:
    if value in RECIPE_OPERATOR_RESPONSE_STATE_VALUES:
        return cast(RecipeOperatorResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_OPERATOR_RESPONSE_STATE_VALUES!r}")
