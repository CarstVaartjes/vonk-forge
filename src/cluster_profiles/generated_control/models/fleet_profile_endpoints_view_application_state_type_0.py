from typing import Literal

FleetProfileEndpointsViewApplicationStateType0 = Literal['cancelled', 'failed', 'needs-operator', 'queued', 'running', 'succeeded', 'superseded']

FLEET_PROFILE_ENDPOINTS_VIEW_APPLICATION_STATE_TYPE_0_VALUES: set[FleetProfileEndpointsViewApplicationStateType0] = { 'cancelled', 'failed', 'needs-operator', 'queued', 'running', 'succeeded', 'superseded',  }

def check_fleet_profile_endpoints_view_application_state_type_0(value: str) -> FleetProfileEndpointsViewApplicationStateType0:
    if value in FLEET_PROFILE_ENDPOINTS_VIEW_APPLICATION_STATE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ENDPOINTS_VIEW_APPLICATION_STATE_TYPE_0_VALUES!r}")
