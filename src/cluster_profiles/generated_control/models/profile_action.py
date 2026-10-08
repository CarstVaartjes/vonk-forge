from typing import Literal

ProfileAction = Literal['adopt', 'keep', 'switch']

PROFILE_ACTION_VALUES: set[ProfileAction] = { 'adopt', 'keep', 'switch',  }

def check_profile_action(value: str) -> ProfileAction:
    if value in PROFILE_ACTION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_ACTION_VALUES!r}")
