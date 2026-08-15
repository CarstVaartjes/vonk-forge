from typing import Literal, cast

RecipePresenceRankState = Literal['failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled']

RECIPE_PRESENCE_RANK_STATE_VALUES: set[RecipePresenceRankState] = { 'failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled',  }

def check_recipe_presence_rank_state(value: str) -> RecipePresenceRankState:
    if value in RECIPE_PRESENCE_RANK_STATE_VALUES:
        return cast(RecipePresenceRankState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PRESENCE_RANK_STATE_VALUES!r}")
