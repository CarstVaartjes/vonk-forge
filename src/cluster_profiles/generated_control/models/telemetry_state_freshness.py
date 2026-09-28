from typing import Literal

TelemetryStateFreshness = Literal['delayed', 'live', 'stale']

TELEMETRY_STATE_FRESHNESS_VALUES: set[TelemetryStateFreshness] = { 'delayed', 'live', 'stale',  }

def check_telemetry_state_freshness(value: str) -> TelemetryStateFreshness:
    if value in TELEMETRY_STATE_FRESHNESS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_STATE_FRESHNESS_VALUES!r}")
