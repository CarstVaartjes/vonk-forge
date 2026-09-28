from typing import Literal

FleetProfileApplicationCancellationViewCause = Literal['operator', 'superseded']

FLEET_PROFILE_APPLICATION_CANCELLATION_VIEW_CAUSE_VALUES: set[FleetProfileApplicationCancellationViewCause] = { 'operator', 'superseded',  }

def check_fleet_profile_application_cancellation_view_cause(value: str) -> FleetProfileApplicationCancellationViewCause:
    if value in FLEET_PROFILE_APPLICATION_CANCELLATION_VIEW_CAUSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_CANCELLATION_VIEW_CAUSE_VALUES!r}")
