from typing import Literal

RecipeJobRunParentOwnerKind = Literal['artifact-job', 'installation', 'recipe-build', 'run']

RECIPE_JOB_RUN_PARENT_OWNER_KIND_VALUES: set[RecipeJobRunParentOwnerKind] = { 'artifact-job', 'installation', 'recipe-build', 'run',  }

def check_recipe_job_run_parent_owner_kind(value: str) -> RecipeJobRunParentOwnerKind:
    if value in RECIPE_JOB_RUN_PARENT_OWNER_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_JOB_RUN_PARENT_OWNER_KIND_VALUES!r}")
