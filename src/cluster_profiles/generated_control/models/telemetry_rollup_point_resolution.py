from typing import Literal

TelemetryRollupPointResolution = Literal['daily', 'fifteen-minute', 'minute']

TELEMETRY_ROLLUP_POINT_RESOLUTION_VALUES: set[TelemetryRollupPointResolution] = { 'daily', 'fifteen-minute', 'minute',  }

def check_telemetry_rollup_point_resolution(value: str) -> TelemetryRollupPointResolution:
    if value in TELEMETRY_ROLLUP_POINT_RESOLUTION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_ROLLUP_POINT_RESOLUTION_VALUES!r}")
