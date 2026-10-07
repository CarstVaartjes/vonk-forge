from typing import Literal

FleetRefreshEventResetReason = Literal['cursor-ahead', 'frame-unavailable', 'initial', 'missing-telemetry-sample', 'retention-gap']

FLEET_REFRESH_EVENT_RESET_REASON_VALUES: set[FleetRefreshEventResetReason] = { 'cursor-ahead', 'frame-unavailable', 'initial', 'missing-telemetry-sample', 'retention-gap',  }

def check_fleet_refresh_event_reset_reason(value: str) -> FleetRefreshEventResetReason:
    if value in FLEET_REFRESH_EVENT_RESET_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_REFRESH_EVENT_RESET_REASON_VALUES!r}")
