from typing import Literal

RecipeBuildCleanupParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_BUILD_CLEANUP_PARENT_OWNER_KIND_VALUES: set[RecipeBuildCleanupParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_build_cleanup_parent_owner_kind(value: str) -> RecipeBuildCleanupParentOwnerKind:
    if value in RECIPE_BUILD_CLEANUP_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_CLEANUP_PARENT_OWNER_KIND_VALUES!r}")
