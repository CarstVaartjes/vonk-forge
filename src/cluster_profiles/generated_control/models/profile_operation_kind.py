from typing import Literal

ProfileOperationKind = Literal['fleet-profile.apply']

PROFILE_OPERATION_KIND_VALUES: set[ProfileOperationKind] = { 'fleet-profile.apply',  }

def check_profile_operation_kind(value: str) -> ProfileOperationKind:
    if value in PROFILE_OPERATION_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_OPERATION_KIND_VALUES!r}")
