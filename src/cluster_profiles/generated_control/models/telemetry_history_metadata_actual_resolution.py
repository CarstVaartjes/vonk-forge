from typing import Literal, cast

TelemetryHistoryMetadataActualResolution = Literal['daily', 'fifteen-minute', 'minute', 'raw']

TELEMETRY_HISTORY_METADATA_ACTUAL_RESOLUTION_VALUES: set[TelemetryHistoryMetadataActualResolution] = { 'daily', 'fifteen-minute', 'minute', 'raw',  }

def check_telemetry_history_metadata_actual_resolution(value: str) -> TelemetryHistoryMetadataActualResolution:
    if value in TELEMETRY_HISTORY_METADATA_ACTUAL_RESOLUTION_VALUES:
        return cast(TelemetryHistoryMetadataActualResolution, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_HISTORY_METADATA_ACTUAL_RESOLUTION_VALUES!r}")
