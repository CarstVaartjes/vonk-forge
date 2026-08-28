from typing import Literal, cast

FleetProfileApplicationViewState = Literal['cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator']

FLEET_PROFILE_APPLICATION_VIEW_STATE_VALUES: set[FleetProfileApplicationViewState] = { 'cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator',  }

def check_fleet_profile_application_view_state(value: str) -> FleetProfileApplicationViewState:
    if value in FLEET_PROFILE_APPLICATION_VIEW_STATE_VALUES:
        return cast(FleetProfileApplicationViewState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_VIEW_STATE_VALUES!r}")
