from typing import Literal

FleetProfileAssignmentDesiredState = Literal['installed', 'running']

FLEET_PROFILE_ASSIGNMENT_DESIRED_STATE_VALUES: set[FleetProfileAssignmentDesiredState] = { 'installed', 'running',  }

def check_fleet_profile_assignment_desired_state(value: str) -> FleetProfileAssignmentDesiredState:
    if value in FLEET_PROFILE_ASSIGNMENT_DESIRED_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_DESIRED_STATE_VALUES!r}")
