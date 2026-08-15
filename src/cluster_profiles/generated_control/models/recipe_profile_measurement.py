from typing import Literal, cast

RecipeProfileMeasurement = Literal['declared', 'derived', 'measured']

RECIPE_PROFILE_MEASUREMENT_VALUES: set[RecipeProfileMeasurement] = { 'declared', 'derived', 'measured',  }

def check_recipe_profile_measurement(value: str) -> RecipeProfileMeasurement:
    if value in RECIPE_PROFILE_MEASUREMENT_VALUES:
        return cast(RecipeProfileMeasurement, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PROFILE_MEASUREMENT_VALUES!r}")
