from typing import Literal, cast

RecipePresenceGroupState = Literal['failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled']

RECIPE_PRESENCE_GROUP_STATE_VALUES: set[RecipePresenceGroupState] = { 'failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled',  }

def check_recipe_presence_group_state(value: str) -> RecipePresenceGroupState:
    if value in RECIPE_PRESENCE_GROUP_STATE_VALUES:
        return cast(RecipePresenceGroupState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PRESENCE_GROUP_STATE_VALUES!r}")
