from typing import Literal

GetMetricsSeriesRange = Literal['1h', '24h', '6h', '7d']

GET_METRICS_SERIES_RANGE_VALUES: set[GetMetricsSeriesRange] = { '1h', '24h', '6h', '7d',  }

def check_get_metrics_series_range(value: str) -> GetMetricsSeriesRange:
    if value in GET_METRICS_SERIES_RANGE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GET_METRICS_SERIES_RANGE_VALUES!r}")
