from typing import Literal, cast

RecipeMemoryResourcesKind = Literal['accelerator', 'host', 'unified']

RECIPE_MEMORY_RESOURCES_KIND_VALUES: set[RecipeMemoryResourcesKind] = { 'accelerator', 'host', 'unified',  }

def check_recipe_memory_resources_kind(value: str) -> RecipeMemoryResourcesKind:
    if value in RECIPE_MEMORY_RESOURCES_KIND_VALUES:
        return cast(RecipeMemoryResourcesKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_MEMORY_RESOURCES_KIND_VALUES!r}")
