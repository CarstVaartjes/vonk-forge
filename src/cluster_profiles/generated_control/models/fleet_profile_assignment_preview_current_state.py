from typing import Literal

FleetProfileAssignmentPreviewCurrentState = Literal['degraded', 'installed', 'installing', 'not-placed', 'placed', 'running']

FLEET_PROFILE_ASSIGNMENT_PREVIEW_CURRENT_STATE_VALUES: set[FleetProfileAssignmentPreviewCurrentState] = { 'degraded', 'installed', 'installing', 'not-placed', 'placed', 'running',  }

def check_fleet_profile_assignment_preview_current_state(value: str) -> FleetProfileAssignmentPreviewCurrentState:
    if value in FLEET_PROFILE_ASSIGNMENT_PREVIEW_CURRENT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_PREVIEW_CURRENT_STATE_VALUES!r}")
