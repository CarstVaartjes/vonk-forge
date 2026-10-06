from typing import Literal

FleetProfileApplicationCancellationViewState = Literal['cancelled', 'observing']

FLEET_PROFILE_APPLICATION_CANCELLATION_VIEW_STATE_VALUES: set[FleetProfileApplicationCancellationViewState] = { 'cancelled', 'observing',  }

def check_fleet_profile_application_cancellation_view_state(value: str) -> FleetProfileApplicationCancellationViewState:
    if value in FLEET_PROFILE_APPLICATION_CANCELLATION_VIEW_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_CANCELLATION_VIEW_STATE_VALUES!r}")
