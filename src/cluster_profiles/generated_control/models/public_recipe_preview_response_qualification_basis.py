from typing import Literal, cast

PublicRecipePreviewResponseQualificationBasis = Literal['conflicting-metadata', 'explicit-accepted-metadata', 'explicit-candidate-metadata', 'missing-accepted-metadata']

PUBLIC_RECIPE_PREVIEW_RESPONSE_QUALIFICATION_BASIS_VALUES: set[PublicRecipePreviewResponseQualificationBasis] = { 'conflicting-metadata', 'explicit-accepted-metadata', 'explicit-candidate-metadata', 'missing-accepted-metadata',  }

def check_public_recipe_preview_response_qualification_basis(value: str) -> PublicRecipePreviewResponseQualificationBasis:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_QUALIFICATION_BASIS_VALUES:
        return cast(PublicRecipePreviewResponseQualificationBasis, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_QUALIFICATION_BASIS_VALUES!r}")
