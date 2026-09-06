from typing import Literal, cast

RecipeIntegerSettingChangeEffect = Literal['none', 'rebuild', 'reprepare', 'restart']

RECIPE_INTEGER_SETTING_CHANGE_EFFECT_VALUES: set[RecipeIntegerSettingChangeEffect] = { 'none', 'rebuild', 'reprepare', 'restart',  }

def check_recipe_integer_setting_change_effect(value: str) -> RecipeIntegerSettingChangeEffect:
    if value in RECIPE_INTEGER_SETTING_CHANGE_EFFECT_VALUES:
        return cast(RecipeIntegerSettingChangeEffect, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_INTEGER_SETTING_CHANGE_EFFECT_VALUES!r}")
