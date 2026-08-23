from typing import Literal, cast

PublicRecipePreviewResponseQualification = Literal['candidate', 'cataloged']

PUBLIC_RECIPE_PREVIEW_RESPONSE_QUALIFICATION_VALUES: set[PublicRecipePreviewResponseQualification] = { 'candidate', 'cataloged',  }

def check_public_recipe_preview_response_qualification(value: str) -> PublicRecipePreviewResponseQualification:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_QUALIFICATION_VALUES:
        return cast(PublicRecipePreviewResponseQualification, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_QUALIFICATION_VALUES!r}")
