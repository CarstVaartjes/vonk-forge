from typing import Literal, cast

PublicRecipeListItemAlignment = Literal['abliterated', 'derisked', 'other-modified', 'standard', 'unspecified']

PUBLIC_RECIPE_LIST_ITEM_ALIGNMENT_VALUES: set[PublicRecipeListItemAlignment] = { 'abliterated', 'derisked', 'other-modified', 'standard', 'unspecified',  }

def check_public_recipe_list_item_alignment(value: str) -> PublicRecipeListItemAlignment:
    if value in PUBLIC_RECIPE_LIST_ITEM_ALIGNMENT_VALUES:
        return cast(PublicRecipeListItemAlignment, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LIST_ITEM_ALIGNMENT_VALUES!r}")
