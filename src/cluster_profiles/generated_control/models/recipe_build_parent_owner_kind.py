from typing import Literal

RecipeBuildParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_BUILD_PARENT_OWNER_KIND_VALUES: set[RecipeBuildParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_build_parent_owner_kind(value: str) -> RecipeBuildParentOwnerKind:
    if value in RECIPE_BUILD_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_PARENT_OWNER_KIND_VALUES!r}")
