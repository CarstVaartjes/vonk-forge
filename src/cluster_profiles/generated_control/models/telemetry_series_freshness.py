from typing import Literal, cast

TelemetrySeriesFreshness = Literal['delayed', 'fresh', 'stale']

TELEMETRY_SERIES_FRESHNESS_VALUES: set[TelemetrySeriesFreshness] = { 'delayed', 'fresh', 'stale',  }

def check_telemetry_series_freshness(value: str) -> TelemetrySeriesFreshness:
    if value in TELEMETRY_SERIES_FRESHNESS_VALUES:
        return cast(TelemetrySeriesFreshness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_SERIES_FRESHNESS_VALUES!r}")
