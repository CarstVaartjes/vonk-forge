from typing import Literal, cast

PublicRecipePreviewResponseCapabilitiesItem = Literal['3d', 'audio', 'chat', 'image-editing', 'image-generation', 'reasoning', 'video', 'vision']

PUBLIC_RECIPE_PREVIEW_RESPONSE_CAPABILITIES_ITEM_VALUES: set[PublicRecipePreviewResponseCapabilitiesItem] = { '3d', 'audio', 'chat', 'image-editing', 'image-generation', 'reasoning', 'video', 'vision',  }

def check_public_recipe_preview_response_capabilities_item(value: str) -> PublicRecipePreviewResponseCapabilitiesItem:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_CAPABILITIES_ITEM_VALUES:
        return cast(PublicRecipePreviewResponseCapabilitiesItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_CAPABILITIES_ITEM_VALUES!r}")
