from typing import Literal, cast

PublicRecipeFabricConnectivity = Literal['connected', 'full_mesh', 'none', 'switch']

PUBLIC_RECIPE_FABRIC_CONNECTIVITY_VALUES: set[PublicRecipeFabricConnectivity] = { 'connected', 'full_mesh', 'none', 'switch',  }

def check_public_recipe_fabric_connectivity(value: str) -> PublicRecipeFabricConnectivity:
    if value in PUBLIC_RECIPE_FABRIC_CONNECTIVITY_VALUES:
        return cast(PublicRecipeFabricConnectivity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_FABRIC_CONNECTIVITY_VALUES!r}")
