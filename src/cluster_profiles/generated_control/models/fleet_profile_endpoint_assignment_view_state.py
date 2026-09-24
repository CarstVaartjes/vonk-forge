from typing import Literal, cast

FleetProfileEndpointAssignmentViewState = Literal['expired', 'installed-only', 'not-published-yet', 'published', 'unavailable', 'withdrawn']

FLEET_PROFILE_ENDPOINT_ASSIGNMENT_VIEW_STATE_VALUES: set[FleetProfileEndpointAssignmentViewState] = { 'expired', 'installed-only', 'not-published-yet', 'published', 'unavailable', 'withdrawn',  }

def check_fleet_profile_endpoint_assignment_view_state(value: str) -> FleetProfileEndpointAssignmentViewState:
    if value in FLEET_PROFILE_ENDPOINT_ASSIGNMENT_VIEW_STATE_VALUES:
        return cast(FleetProfileEndpointAssignmentViewState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ENDPOINT_ASSIGNMENT_VIEW_STATE_VALUES!r}")
