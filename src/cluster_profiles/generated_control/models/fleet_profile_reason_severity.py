from typing import Literal, cast

FleetProfileReasonSeverity = Literal['error', 'info', 'warning']

FLEET_PROFILE_REASON_SEVERITY_VALUES: set[FleetProfileReasonSeverity] = { 'error', 'info', 'warning',  }

def check_fleet_profile_reason_severity(value: str) -> FleetProfileReasonSeverity:
    if value in FLEET_PROFILE_REASON_SEVERITY_VALUES:
        return cast(FleetProfileReasonSeverity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_REASON_SEVERITY_VALUES!r}")
