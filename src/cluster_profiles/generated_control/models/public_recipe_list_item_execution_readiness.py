from typing import Literal, cast

PublicRecipeListItemExecutionReadiness = Literal['executable', 'integration-required', 'not-declared', 'not-executable']

PUBLIC_RECIPE_LIST_ITEM_EXECUTION_READINESS_VALUES: set[PublicRecipeListItemExecutionReadiness] = { 'executable', 'integration-required', 'not-declared', 'not-executable',  }

def check_public_recipe_list_item_execution_readiness(value: str) -> PublicRecipeListItemExecutionReadiness:
    if value in PUBLIC_RECIPE_LIST_ITEM_EXECUTION_READINESS_VALUES:
        return cast(PublicRecipeListItemExecutionReadiness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LIST_ITEM_EXECUTION_READINESS_VALUES!r}")
