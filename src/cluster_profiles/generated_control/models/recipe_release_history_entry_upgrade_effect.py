from typing import Literal, cast

RecipeReleaseHistoryEntryUpgradeEffect = Literal['none', 'rebuild', 'reprepare', 'restart']

RECIPE_RELEASE_HISTORY_ENTRY_UPGRADE_EFFECT_VALUES: set[RecipeReleaseHistoryEntryUpgradeEffect] = { 'none', 'rebuild', 'reprepare', 'restart',  }

def check_recipe_release_history_entry_upgrade_effect(value: str) -> RecipeReleaseHistoryEntryUpgradeEffect:
    if value in RECIPE_RELEASE_HISTORY_ENTRY_UPGRADE_EFFECT_VALUES:
        return cast(RecipeReleaseHistoryEntryUpgradeEffect, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_RELEASE_HISTORY_ENTRY_UPGRADE_EFFECT_VALUES!r}")
