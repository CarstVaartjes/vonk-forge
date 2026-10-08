from typing import Literal

ProfileSwitchChildKind = Literal['cleanup', 'install', 'run', 'stop']

PROFILE_SWITCH_CHILD_KIND_VALUES: set[ProfileSwitchChildKind] = { 'cleanup', 'install', 'run', 'stop',  }

def check_profile_switch_child_kind(value: str) -> ProfileSwitchChildKind:
    if value in PROFILE_SWITCH_CHILD_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_SWITCH_CHILD_KIND_VALUES!r}")
