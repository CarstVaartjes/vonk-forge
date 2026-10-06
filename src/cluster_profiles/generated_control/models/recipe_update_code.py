from typing import Literal

RecipeUpdateCode = Literal['recipe-update.cancel-effect-unknown', 'recipe_update.claim_lost', 'recipe_update.observation_invalid', 'recipe_update.operation_invalid', 'recipe_update.request_key_reused', 'recipe_update.scope_invalid']

RECIPE_UPDATE_CODE_VALUES: set[RecipeUpdateCode] = { 'recipe-update.cancel-effect-unknown', 'recipe_update.claim_lost', 'recipe_update.observation_invalid', 'recipe_update.operation_invalid', 'recipe_update.request_key_reused', 'recipe_update.scope_invalid',  }

def check_recipe_update_code(value: str) -> RecipeUpdateCode:
    if value in RECIPE_UPDATE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_UPDATE_CODE_VALUES!r}")
