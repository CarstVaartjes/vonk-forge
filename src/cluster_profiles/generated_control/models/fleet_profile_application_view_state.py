from typing import Literal

FleetProfileApplicationViewState = Literal['cancelled', 'failed', 'needs-operator', 'queued', 'running', 'succeeded', 'superseded']

FLEET_PROFILE_APPLICATION_VIEW_STATE_VALUES: set[FleetProfileApplicationViewState] = { 'cancelled', 'failed', 'needs-operator', 'queued', 'running', 'succeeded', 'superseded',  }

def check_fleet_profile_application_view_state(value: str) -> FleetProfileApplicationViewState:
    if value in FLEET_PROFILE_APPLICATION_VIEW_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_VIEW_STATE_VALUES!r}")
