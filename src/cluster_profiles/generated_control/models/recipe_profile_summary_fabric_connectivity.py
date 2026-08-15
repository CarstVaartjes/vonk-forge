from typing import Literal, cast

RecipeProfileSummaryFabricConnectivity = Literal['connected', 'full_mesh', 'none', 'switch']

RECIPE_PROFILE_SUMMARY_FABRIC_CONNECTIVITY_VALUES: set[RecipeProfileSummaryFabricConnectivity] = { 'connected', 'full_mesh', 'none', 'switch',  }

def check_recipe_profile_summary_fabric_connectivity(value: str) -> RecipeProfileSummaryFabricConnectivity:
    if value in RECIPE_PROFILE_SUMMARY_FABRIC_CONNECTIVITY_VALUES:
        return cast(RecipeProfileSummaryFabricConnectivity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PROFILE_SUMMARY_FABRIC_CONNECTIVITY_VALUES!r}")
