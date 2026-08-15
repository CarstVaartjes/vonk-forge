from typing import Literal, cast

GetNodeTelemetryHistoryResolution = Literal['fifteen-minute', 'minute', 'raw']

GET_NODE_TELEMETRY_HISTORY_RESOLUTION_VALUES: set[GetNodeTelemetryHistoryResolution] = { 'fifteen-minute', 'minute', 'raw',  }

def check_get_node_telemetry_history_resolution(value: str) -> GetNodeTelemetryHistoryResolution:
    if value in GET_NODE_TELEMETRY_HISTORY_RESOLUTION_VALUES:
        return cast(GetNodeTelemetryHistoryResolution, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GET_NODE_TELEMETRY_HISTORY_RESOLUTION_VALUES!r}")
