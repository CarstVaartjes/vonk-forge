from typing import Literal, cast

PublicRecipeListItemQualification = Literal['candidate', 'cataloged']

PUBLIC_RECIPE_LIST_ITEM_QUALIFICATION_VALUES: set[PublicRecipeListItemQualification] = { 'candidate', 'cataloged',  }

def check_public_recipe_list_item_qualification(value: str) -> PublicRecipeListItemQualification:
    if value in PUBLIC_RECIPE_LIST_ITEM_QUALIFICATION_VALUES:
        return cast(PublicRecipeListItemQualification, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LIST_ITEM_QUALIFICATION_VALUES!r}")
