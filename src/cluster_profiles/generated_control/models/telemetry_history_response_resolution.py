from typing import Literal

TelemetryHistoryResponseResolution = Literal['daily', 'fifteen-minute', 'minute', 'raw']

TELEMETRY_HISTORY_RESPONSE_RESOLUTION_VALUES: set[TelemetryHistoryResponseResolution] = { 'daily', 'fifteen-minute', 'minute', 'raw',  }

def check_telemetry_history_response_resolution(value: str) -> TelemetryHistoryResponseResolution:
    if value in TELEMETRY_HISTORY_RESPONSE_RESOLUTION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_HISTORY_RESPONSE_RESOLUTION_VALUES!r}")
