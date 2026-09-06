from typing import Literal, cast

RecipeImageAvailabilityChildState = Literal['failed', 'partial', 'queued', 'running', 'succeeded']

RECIPE_IMAGE_AVAILABILITY_CHILD_STATE_VALUES: set[RecipeImageAvailabilityChildState] = { 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_recipe_image_availability_child_state(value: str) -> RecipeImageAvailabilityChildState:
    if value in RECIPE_IMAGE_AVAILABILITY_CHILD_STATE_VALUES:
        return cast(RecipeImageAvailabilityChildState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_IMAGE_AVAILABILITY_CHILD_STATE_VALUES!r}")
