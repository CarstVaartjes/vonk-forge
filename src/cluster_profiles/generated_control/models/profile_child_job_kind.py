from typing import Literal

ProfileChildJobKind = Literal['recipe.cleanup.v2', 'recipe.run-switch.v2', 'recipe.stop.v2']

PROFILE_CHILD_JOB_KIND_VALUES: set[ProfileChildJobKind] = { 'recipe.cleanup.v2', 'recipe.run-switch.v2', 'recipe.stop.v2',  }

def check_profile_child_job_kind(value: str) -> ProfileChildJobKind:
    if value in PROFILE_CHILD_JOB_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_CHILD_JOB_KIND_VALUES!r}")
