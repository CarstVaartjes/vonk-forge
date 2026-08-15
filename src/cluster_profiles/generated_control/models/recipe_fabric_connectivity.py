from typing import Literal, cast

RecipeFabricConnectivity = Literal['connected', 'full_mesh', 'none', 'switch']

RECIPE_FABRIC_CONNECTIVITY_VALUES: set[RecipeFabricConnectivity] = { 'connected', 'full_mesh', 'none', 'switch',  }

def check_recipe_fabric_connectivity(value: str) -> RecipeFabricConnectivity:
    if value in RECIPE_FABRIC_CONNECTIVITY_VALUES:
        return cast(RecipeFabricConnectivity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_FABRIC_CONNECTIVITY_VALUES!r}")
