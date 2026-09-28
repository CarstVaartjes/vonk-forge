from typing import Literal

RecipeImageAvailabilityChildKind = Literal['model-cache', 'runtime-image']

RECIPE_IMAGE_AVAILABILITY_CHILD_KIND_VALUES: set[RecipeImageAvailabilityChildKind] = { 'model-cache', 'runtime-image',  }

def check_recipe_image_availability_child_kind(value: str) -> RecipeImageAvailabilityChildKind:
    if value in RECIPE_IMAGE_AVAILABILITY_CHILD_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_IMAGE_AVAILABILITY_CHILD_KIND_VALUES!r}")
