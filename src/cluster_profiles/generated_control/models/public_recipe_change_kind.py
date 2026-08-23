from typing import Literal, cast

PublicRecipeChangeKind = Literal['breaking', 'compatibility', 'fix', 'initial', 'metadata', 'model', 'performance', 'runtime', 'security']

PUBLIC_RECIPE_CHANGE_KIND_VALUES: set[PublicRecipeChangeKind] = { 'breaking', 'compatibility', 'fix', 'initial', 'metadata', 'model', 'performance', 'runtime', 'security',  }

def check_public_recipe_change_kind(value: str) -> PublicRecipeChangeKind:
    if value in PUBLIC_RECIPE_CHANGE_KIND_VALUES:
        return cast(PublicRecipeChangeKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_CHANGE_KIND_VALUES!r}")
