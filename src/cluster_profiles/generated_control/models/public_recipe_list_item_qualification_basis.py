from typing import Literal, cast

PublicRecipeListItemQualificationBasis = Literal['conflicting-metadata', 'explicit-accepted-metadata', 'explicit-candidate-metadata', 'missing-accepted-metadata']

PUBLIC_RECIPE_LIST_ITEM_QUALIFICATION_BASIS_VALUES: set[PublicRecipeListItemQualificationBasis] = { 'conflicting-metadata', 'explicit-accepted-metadata', 'explicit-candidate-metadata', 'missing-accepted-metadata',  }

def check_public_recipe_list_item_qualification_basis(value: str) -> PublicRecipeListItemQualificationBasis:
    if value in PUBLIC_RECIPE_LIST_ITEM_QUALIFICATION_BASIS_VALUES:
        return cast(PublicRecipeListItemQualificationBasis, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LIST_ITEM_QUALIFICATION_BASIS_VALUES!r}")
