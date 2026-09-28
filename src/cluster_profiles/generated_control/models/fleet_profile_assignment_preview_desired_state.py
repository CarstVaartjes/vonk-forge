from typing import Literal

FleetProfileAssignmentPreviewDesiredState = Literal['installed', 'running']

FLEET_PROFILE_ASSIGNMENT_PREVIEW_DESIRED_STATE_VALUES: set[FleetProfileAssignmentPreviewDesiredState] = { 'installed', 'running',  }

def check_fleet_profile_assignment_preview_desired_state(value: str) -> FleetProfileAssignmentPreviewDesiredState:
    if value in FLEET_PROFILE_ASSIGNMENT_PREVIEW_DESIRED_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_PREVIEW_DESIRED_STATE_VALUES!r}")
