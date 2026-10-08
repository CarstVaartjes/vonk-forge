from typing import Literal

ProfileProjectionKind = Literal['agent-operation', 'fleet-profile-application', 'job', 'profile-application', 'profile-step']

PROFILE_PROJECTION_KIND_VALUES: set[ProfileProjectionKind] = { 'agent-operation', 'fleet-profile-application', 'job', 'profile-application', 'profile-step',  }

def check_profile_projection_kind(value: str) -> ProfileProjectionKind:
    if value in PROFILE_PROJECTION_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_PROJECTION_KIND_VALUES!r}")
