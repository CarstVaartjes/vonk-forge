from typing import Literal

RecipeInstallParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_INSTALL_PARENT_OWNER_KIND_VALUES: set[RecipeInstallParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_install_parent_owner_kind(value: str) -> RecipeInstallParentOwnerKind:
    if value in RECIPE_INSTALL_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_INSTALL_PARENT_OWNER_KIND_VALUES!r}")
