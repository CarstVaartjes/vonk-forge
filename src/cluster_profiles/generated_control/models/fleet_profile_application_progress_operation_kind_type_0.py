from typing import Literal, cast

FleetProfileApplicationProgressOperationKindType0 = Literal['fleet-profile.apply', 'fleet-profile.prepare']

FLEET_PROFILE_APPLICATION_PROGRESS_OPERATION_KIND_TYPE_0_VALUES: set[FleetProfileApplicationProgressOperationKindType0] = { 'fleet-profile.apply', 'fleet-profile.prepare',  }

def check_fleet_profile_application_progress_operation_kind_type_0(value: str) -> FleetProfileApplicationProgressOperationKindType0:
    if value in FLEET_PROFILE_APPLICATION_PROGRESS_OPERATION_KIND_TYPE_0_VALUES:
        return cast(FleetProfileApplicationProgressOperationKindType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_PROGRESS_OPERATION_KIND_TYPE_0_VALUES!r}")
