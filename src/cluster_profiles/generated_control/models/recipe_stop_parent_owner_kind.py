from typing import Literal

RecipeStopParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_STOP_PARENT_OWNER_KIND_VALUES: set[RecipeStopParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_stop_parent_owner_kind(value: str) -> RecipeStopParentOwnerKind:
    if value in RECIPE_STOP_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_STOP_PARENT_OWNER_KIND_VALUES!r}")
