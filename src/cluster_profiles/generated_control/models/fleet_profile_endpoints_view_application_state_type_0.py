from typing import Literal, cast

FleetProfileEndpointsViewApplicationStateType0 = Literal['cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator']

FLEET_PROFILE_ENDPOINTS_VIEW_APPLICATION_STATE_TYPE_0_VALUES: set[FleetProfileEndpointsViewApplicationStateType0] = { 'cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator',  }

def check_fleet_profile_endpoints_view_application_state_type_0(value: str) -> FleetProfileEndpointsViewApplicationStateType0:
    if value in FLEET_PROFILE_ENDPOINTS_VIEW_APPLICATION_STATE_TYPE_0_VALUES:
        return cast(FleetProfileEndpointsViewApplicationStateType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ENDPOINTS_VIEW_APPLICATION_STATE_TYPE_0_VALUES!r}")
