from typing import Literal, cast

PublicRecipeListItemCapabilitiesItem = Literal['3d', 'audio', 'chat', 'image-editing', 'image-generation', 'reasoning', 'video', 'vision']

PUBLIC_RECIPE_LIST_ITEM_CAPABILITIES_ITEM_VALUES: set[PublicRecipeListItemCapabilitiesItem] = { '3d', 'audio', 'chat', 'image-editing', 'image-generation', 'reasoning', 'video', 'vision',  }

def check_public_recipe_list_item_capabilities_item(value: str) -> PublicRecipeListItemCapabilitiesItem:
    if value in PUBLIC_RECIPE_LIST_ITEM_CAPABILITIES_ITEM_VALUES:
        return cast(PublicRecipeListItemCapabilitiesItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LIST_ITEM_CAPABILITIES_ITEM_VALUES!r}")
