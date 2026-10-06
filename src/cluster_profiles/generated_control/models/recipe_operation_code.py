from typing import Literal

RecipeOperationCode = Literal['recipe.operation_conflict']

RECIPE_OPERATION_CODE_VALUES: set[RecipeOperationCode] = { 'recipe.operation_conflict',  }

def check_recipe_operation_code(value: str) -> RecipeOperationCode:
    if value in RECIPE_OPERATION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_OPERATION_CODE_VALUES!r}")
