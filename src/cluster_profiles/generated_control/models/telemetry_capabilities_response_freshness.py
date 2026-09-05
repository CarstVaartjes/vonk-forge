from typing import Literal, cast

TelemetryCapabilitiesResponseFreshness = Literal['delayed', 'live', 'stale']

TELEMETRY_CAPABILITIES_RESPONSE_FRESHNESS_VALUES: set[TelemetryCapabilitiesResponseFreshness] = { 'delayed', 'live', 'stale',  }

def check_telemetry_capabilities_response_freshness(value: str) -> TelemetryCapabilitiesResponseFreshness:
    if value in TELEMETRY_CAPABILITIES_RESPONSE_FRESHNESS_VALUES:
        return cast(TelemetryCapabilitiesResponseFreshness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_CAPABILITIES_RESPONSE_FRESHNESS_VALUES!r}")
