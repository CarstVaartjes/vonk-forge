from typing import Literal, cast

VisualRecipeParameterType = Literal['boolean', 'enum', 'integer', 'string']

VISUAL_RECIPE_PARAMETER_TYPE_VALUES: set[VisualRecipeParameterType] = { 'boolean', 'enum', 'integer', 'string',  }

def check_visual_recipe_parameter_type(value: str) -> VisualRecipeParameterType:
    if value in VISUAL_RECIPE_PARAMETER_TYPE_VALUES:
        return cast(VisualRecipeParameterType, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_RECIPE_PARAMETER_TYPE_VALUES!r}")
