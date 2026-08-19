from typing import Literal, cast

PublicRecipePreviewResponseSource = Literal['global', 'recipe_library']

PUBLIC_RECIPE_PREVIEW_RESPONSE_SOURCE_VALUES: set[PublicRecipePreviewResponseSource] = { 'global', 'recipe_library',  }

def check_public_recipe_preview_response_source(value: str) -> PublicRecipePreviewResponseSource:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_SOURCE_VALUES:
        return cast(PublicRecipePreviewResponseSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_SOURCE_VALUES!r}")
