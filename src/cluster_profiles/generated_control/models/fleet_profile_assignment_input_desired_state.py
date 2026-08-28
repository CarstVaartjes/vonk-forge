from typing import Literal, cast

FleetProfileAssignmentInputDesiredState = Literal['installed', 'running']

FLEET_PROFILE_ASSIGNMENT_INPUT_DESIRED_STATE_VALUES: set[FleetProfileAssignmentInputDesiredState] = { 'installed', 'running',  }

def check_fleet_profile_assignment_input_desired_state(value: str) -> FleetProfileAssignmentInputDesiredState:
    if value in FLEET_PROFILE_ASSIGNMENT_INPUT_DESIRED_STATE_VALUES:
        return cast(FleetProfileAssignmentInputDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_INPUT_DESIRED_STATE_VALUES!r}")
