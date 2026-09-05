from typing import Literal, cast

TelemetryWorkloadsResponseFreshness = Literal['delayed', 'live', 'stale']

TELEMETRY_WORKLOADS_RESPONSE_FRESHNESS_VALUES: set[TelemetryWorkloadsResponseFreshness] = { 'delayed', 'live', 'stale',  }

def check_telemetry_workloads_response_freshness(value: str) -> TelemetryWorkloadsResponseFreshness:
    if value in TELEMETRY_WORKLOADS_RESPONSE_FRESHNESS_VALUES:
        return cast(TelemetryWorkloadsResponseFreshness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_WORKLOADS_RESPONSE_FRESHNESS_VALUES!r}")
