from typing import Literal

RecipeAlternativeFitsFleet = Literal['blocked', 'ready', 'unavailable']

RECIPE_ALTERNATIVE_FITS_FLEET_VALUES: set[RecipeAlternativeFitsFleet] = { 'blocked', 'ready', 'unavailable',  }

def check_recipe_alternative_fits_fleet(value: str) -> RecipeAlternativeFitsFleet:
    if value in RECIPE_ALTERNATIVE_FITS_FLEET_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_ALTERNATIVE_FITS_FLEET_VALUES!r}")
