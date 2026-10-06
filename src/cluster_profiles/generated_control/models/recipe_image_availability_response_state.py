from typing import Literal

RecipeImageAvailabilityResponseState = Literal['backoff', 'cancelled', 'failed', 'observing', 'queued', 'running', 'succeeded']

RECIPE_IMAGE_AVAILABILITY_RESPONSE_STATE_VALUES: set[RecipeImageAvailabilityResponseState] = { 'backoff', 'cancelled', 'failed', 'observing', 'queued', 'running', 'succeeded',  }

def check_recipe_image_availability_response_state(value: str) -> RecipeImageAvailabilityResponseState:
    if value in RECIPE_IMAGE_AVAILABILITY_RESPONSE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_IMAGE_AVAILABILITY_RESPONSE_STATE_VALUES!r}")
