from typing import Literal, cast

FleetProfileApplicationCancellationIntentCause = Literal['operator', 'superseded']

FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_CAUSE_VALUES: set[FleetProfileApplicationCancellationIntentCause] = { 'operator', 'superseded',  }

def check_fleet_profile_application_cancellation_intent_cause(value: str) -> FleetProfileApplicationCancellationIntentCause:
    if value in FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_CAUSE_VALUES:
        return cast(FleetProfileApplicationCancellationIntentCause, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_CANCELLATION_INTENT_CAUSE_VALUES!r}")
