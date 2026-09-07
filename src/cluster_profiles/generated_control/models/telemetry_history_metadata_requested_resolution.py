from typing import Literal, cast

TelemetryHistoryMetadataRequestedResolution = Literal['daily', 'fifteen-minute', 'minute', 'raw']

TELEMETRY_HISTORY_METADATA_REQUESTED_RESOLUTION_VALUES: set[TelemetryHistoryMetadataRequestedResolution] = { 'daily', 'fifteen-minute', 'minute', 'raw',  }

def check_telemetry_history_metadata_requested_resolution(value: str) -> TelemetryHistoryMetadataRequestedResolution:
    if value in TELEMETRY_HISTORY_METADATA_REQUESTED_RESOLUTION_VALUES:
        return cast(TelemetryHistoryMetadataRequestedResolution, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_HISTORY_METADATA_REQUESTED_RESOLUTION_VALUES!r}")
