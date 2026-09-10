from typing import Literal, cast

GetFleetMetricsHistoryResolution = Literal['daily', 'fifteen-minute', 'minute', 'raw']

GET_FLEET_METRICS_HISTORY_RESOLUTION_VALUES: set[GetFleetMetricsHistoryResolution] = { 'daily', 'fifteen-minute', 'minute', 'raw',  }

def check_get_fleet_metrics_history_resolution(value: str) -> GetFleetMetricsHistoryResolution:
    if value in GET_FLEET_METRICS_HISTORY_RESOLUTION_VALUES:
        return cast(GetFleetMetricsHistoryResolution, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GET_FLEET_METRICS_HISTORY_RESOLUTION_VALUES!r}")
