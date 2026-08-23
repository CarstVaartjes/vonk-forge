from typing import Literal, cast

PublicRecipeLocalStateStatus = Literal['conflict', 'current', 'different-revision', 'local-ahead', 'not-imported', 'update-available']

PUBLIC_RECIPE_LOCAL_STATE_STATUS_VALUES: set[PublicRecipeLocalStateStatus] = { 'conflict', 'current', 'different-revision', 'local-ahead', 'not-imported', 'update-available',  }

def check_public_recipe_local_state_status(value: str) -> PublicRecipeLocalStateStatus:
    if value in PUBLIC_RECIPE_LOCAL_STATE_STATUS_VALUES:
        return cast(PublicRecipeLocalStateStatus, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_LOCAL_STATE_STATUS_VALUES!r}")
