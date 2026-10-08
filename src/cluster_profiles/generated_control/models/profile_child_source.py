from typing import Literal

ProfileChildSource = Literal['switch-adapter']

PROFILE_CHILD_SOURCE_VALUES: set[ProfileChildSource] = { 'switch-adapter',  }

def check_profile_child_source(value: str) -> ProfileChildSource:
    if value in PROFILE_CHILD_SOURCE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_CHILD_SOURCE_VALUES!r}")
