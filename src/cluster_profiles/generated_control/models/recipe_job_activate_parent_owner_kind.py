from typing import Literal

RecipeJobActivateParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_JOB_ACTIVATE_PARENT_OWNER_KIND_VALUES: set[RecipeJobActivateParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_job_activate_parent_owner_kind(value: str) -> RecipeJobActivateParentOwnerKind:
    if value in RECIPE_JOB_ACTIVATE_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_JOB_ACTIVATE_PARENT_OWNER_KIND_VALUES!r}")
