from typing import Literal

RecipeRunDispositionValue = Literal['unowned']

RECIPE_RUN_DISPOSITION_VALUE_VALUES: set[RecipeRunDispositionValue] = { 'unowned',  }

def check_recipe_run_disposition_value(value: str) -> RecipeRunDispositionValue:
    if value in RECIPE_RUN_DISPOSITION_VALUE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_RUN_DISPOSITION_VALUE_VALUES!r}")
