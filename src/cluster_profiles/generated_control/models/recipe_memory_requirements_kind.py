from typing import Literal, cast

RecipeMemoryRequirementsKind = Literal['accelerator', 'host', 'unified']

RECIPE_MEMORY_REQUIREMENTS_KIND_VALUES: set[RecipeMemoryRequirementsKind] = { 'accelerator', 'host', 'unified',  }

def check_recipe_memory_requirements_kind(value: str) -> RecipeMemoryRequirementsKind:
    if value in RECIPE_MEMORY_REQUIREMENTS_KIND_VALUES:
        return cast(RecipeMemoryRequirementsKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_MEMORY_REQUIREMENTS_KIND_VALUES!r}")
