from typing import Literal

RecipeUninstallParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_UNINSTALL_PARENT_OWNER_KIND_VALUES: set[RecipeUninstallParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_uninstall_parent_owner_kind(value: str) -> RecipeUninstallParentOwnerKind:
    if value in RECIPE_UNINSTALL_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_UNINSTALL_PARENT_OWNER_KIND_VALUES!r}")
