from typing import Literal, cast

PublicRecipeListItemExecutionReadinessBasis = Literal['conflicting-readiness-metadata', 'explicit-executable-metadata', 'explicit-integration-required-metadata', 'explicit-non-executable-metadata', 'missing-readiness-metadata']

PUBLIC_RECIPE_LIST_ITEM_EXECUTION_READINESS_BASIS_VALUES: set[PublicRecipeListItemExecutionReadinessBasis] = { 'conflicting-readiness-metadata', 'explicit-executable-metadata', 'explicit-integration-required-metadata', 'explicit-non-executable-metadata', 'missing-readiness-metadata',  }

def check_public_recipe_list_item_execution_readiness_basis(value: str) -> PublicRecipeListItemExecutionReadinessBasis:
    if value in PUBLIC_RECIPE_LIST_ITEM_EXECUTION_READINESS_BASIS_VALUES:
        return cast(PublicRecipeListItemExecutionReadinessBasis, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LIST_ITEM_EXECUTION_READINESS_BASIS_VALUES!r}")
