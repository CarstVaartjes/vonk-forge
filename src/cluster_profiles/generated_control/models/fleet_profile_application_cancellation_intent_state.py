from typing import Literal

FleetProfileApplicationCancellationIntentState = Literal['cancelled', 'observing']

FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_STATE_VALUES: set[FleetProfileApplicationCancellationIntentState] = { 'cancelled', 'observing',  }

def check_fleet_profile_application_cancellation_intent_state(value: str) -> FleetProfileApplicationCancellationIntentState:
    if value in FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_STATE_VALUES!r}")
