from typing import Literal, cast

TelemetryCurrentResponseFreshness = Literal['delayed', 'live', 'stale']

TELEMETRY_CURRENT_RESPONSE_FRESHNESS_VALUES: set[TelemetryCurrentResponseFreshness] = { 'delayed', 'live', 'stale',  }

def check_telemetry_current_response_freshness(value: str) -> TelemetryCurrentResponseFreshness:
    if value in TELEMETRY_CURRENT_RESPONSE_FRESHNESS_VALUES:
        return cast(TelemetryCurrentResponseFreshness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_CURRENT_RESPONSE_FRESHNESS_VALUES!r}")
