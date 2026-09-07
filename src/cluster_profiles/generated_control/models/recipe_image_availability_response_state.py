from typing import Literal, cast

RecipeImageAvailabilityResponseState = Literal['failed', 'partial', 'queued', 'running', 'succeeded']

RECIPE_IMAGE_AVAILABILITY_RESPONSE_STATE_VALUES: set[RecipeImageAvailabilityResponseState] = { 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_recipe_image_availability_response_state(value: str) -> RecipeImageAvailabilityResponseState:
    if value in RECIPE_IMAGE_AVAILABILITY_RESPONSE_STATE_VALUES:
        return cast(RecipeImageAvailabilityResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_IMAGE_AVAILABILITY_RESPONSE_STATE_VALUES!r}")
