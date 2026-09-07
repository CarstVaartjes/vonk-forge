from typing import Literal, cast

ListRecipeImageAvailabilityStateType0 = Literal['failed', 'partial', 'queued', 'running', 'succeeded']

LIST_RECIPE_IMAGE_AVAILABILITY_STATE_TYPE_0_VALUES: set[ListRecipeImageAvailabilityStateType0] = { 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_list_recipe_image_availability_state_type_0(value: str) -> ListRecipeImageAvailabilityStateType0:
    if value in LIST_RECIPE_IMAGE_AVAILABILITY_STATE_TYPE_0_VALUES:
        return cast(ListRecipeImageAvailabilityStateType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIST_RECIPE_IMAGE_AVAILABILITY_STATE_TYPE_0_VALUES!r}")
