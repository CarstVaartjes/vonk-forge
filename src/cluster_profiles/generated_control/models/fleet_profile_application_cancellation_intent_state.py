from typing import Literal, cast

FleetProfileApplicationCancellationIntentState = Literal['cancelled', 'cancelling']

FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_STATE_VALUES: set[FleetProfileApplicationCancellationIntentState] = { 'cancelled', 'cancelling',  }

def check_fleet_profile_application_cancellation_intent_state(value: str) -> FleetProfileApplicationCancellationIntentState:
    if value in FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_STATE_VALUES:
        return cast(FleetProfileApplicationCancellationIntentState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_STATE_VALUES!r}")
