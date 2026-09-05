from typing import Literal, cast

TelemetrySeriesSupportStatus = Literal['available', 'stale', 'unavailable', 'unsupported']

TELEMETRY_SERIES_SUPPORT_STATUS_VALUES: set[TelemetrySeriesSupportStatus] = { 'available', 'stale', 'unavailable', 'unsupported',  }

def check_telemetry_series_support_status(value: str) -> TelemetrySeriesSupportStatus:
    if value in TELEMETRY_SERIES_SUPPORT_STATUS_VALUES:
        return cast(TelemetrySeriesSupportStatus, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_SERIES_SUPPORT_STATUS_VALUES!r}")
