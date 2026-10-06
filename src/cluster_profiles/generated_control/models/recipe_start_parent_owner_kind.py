from typing import Literal

RecipeStartParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_START_PARENT_OWNER_KIND_VALUES: set[RecipeStartParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_start_parent_owner_kind(value: str) -> RecipeStartParentOwnerKind:
    if value in RECIPE_START_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_START_PARENT_OWNER_KIND_VALUES!r}")
