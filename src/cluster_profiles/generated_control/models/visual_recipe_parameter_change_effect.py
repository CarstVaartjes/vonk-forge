from typing import Literal, cast

VisualRecipeParameterChangeEffect = Literal['rebuild', 'reinstall', 'restart']

VISUAL_RECIPE_PARAMETER_CHANGE_EFFECT_VALUES: set[VisualRecipeParameterChangeEffect] = { 'rebuild', 'reinstall', 'restart',  }

def check_visual_recipe_parameter_change_effect(value: str) -> VisualRecipeParameterChangeEffect:
    if value in VISUAL_RECIPE_PARAMETER_CHANGE_EFFECT_VALUES:
        return cast(VisualRecipeParameterChangeEffect, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_RECIPE_PARAMETER_CHANGE_EFFECT_VALUES!r}")
