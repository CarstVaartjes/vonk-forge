from typing import Literal

RecipeReconcileParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_RECONCILE_PARENT_OWNER_KIND_VALUES: set[RecipeReconcileParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_reconcile_parent_owner_kind(value: str) -> RecipeReconcileParentOwnerKind:
    if value in RECIPE_RECONCILE_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_RECONCILE_PARENT_OWNER_KIND_VALUES!r}")
