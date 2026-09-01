from typing import Literal, cast

PublicRecipePreviewResponseAlignment = Literal['abliterated', 'derisked', 'other-modified', 'standard', 'unspecified']

PUBLIC_RECIPE_PREVIEW_RESPONSE_ALIGNMENT_VALUES: set[PublicRecipePreviewResponseAlignment] = { 'abliterated', 'derisked', 'other-modified', 'standard', 'unspecified',  }

def check_public_recipe_preview_response_alignment(value: str) -> PublicRecipePreviewResponseAlignment:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_ALIGNMENT_VALUES:
        return cast(PublicRecipePreviewResponseAlignment, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_ALIGNMENT_VALUES!r}")
