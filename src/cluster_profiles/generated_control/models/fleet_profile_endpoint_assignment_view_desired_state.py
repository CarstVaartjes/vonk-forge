from typing import Literal, cast

FleetProfileEndpointAssignmentViewDesiredState = Literal['installed', 'running']

FLEET_PROFILE_ENDPOINT_ASSIGNMENT_VIEW_DESIRED_STATE_VALUES: set[FleetProfileEndpointAssignmentViewDesiredState] = { 'installed', 'running',  }

def check_fleet_profile_endpoint_assignment_view_desired_state(value: str) -> FleetProfileEndpointAssignmentViewDesiredState:
    if value in FLEET_PROFILE_ENDPOINT_ASSIGNMENT_VIEW_DESIRED_STATE_VALUES:
        return cast(FleetProfileEndpointAssignmentViewDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ENDPOINT_ASSIGNMENT_VIEW_DESIRED_STATE_VALUES!r}")
