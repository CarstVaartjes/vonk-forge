from typing import Literal

RecipeImageAvailabilityChildState = Literal['backoff', 'cancelled', 'failed', 'observing', 'queued', 'running', 'succeeded']

RECIPE_IMAGE_AVAILABILITY_CHILD_STATE_VALUES: set[RecipeImageAvailabilityChildState] = { 'backoff', 'cancelled', 'failed', 'observing', 'queued', 'running', 'succeeded',  }

def check_recipe_image_availability_child_state(value: str) -> RecipeImageAvailabilityChildState:
    if value in RECIPE_IMAGE_AVAILABILITY_CHILD_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_IMAGE_AVAILABILITY_CHILD_STATE_VALUES!r}")
