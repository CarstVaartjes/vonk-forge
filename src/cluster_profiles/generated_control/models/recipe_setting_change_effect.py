from typing import Literal

RecipeSettingChangeEffect = Literal['none', 'rebuild', 'reprepare', 'restart']

RECIPE_SETTING_CHANGE_EFFECT_VALUES: set[RecipeSettingChangeEffect] = { 'none', 'rebuild', 'reprepare', 'restart',  }

def check_recipe_setting_change_effect(value: str) -> RecipeSettingChangeEffect:
    if value in RECIPE_SETTING_CHANGE_EFFECT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_SETTING_CHANGE_EFFECT_VALUES!r}")
