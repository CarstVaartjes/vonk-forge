from typing import Literal

MetricsSeriesResponseRange = Literal['1h', '24h', '6h', '7d']

METRICS_SERIES_RESPONSE_RANGE_VALUES: set[MetricsSeriesResponseRange] = { '1h', '24h', '6h', '7d',  }

def check_metrics_series_response_range(value: str) -> MetricsSeriesResponseRange:
    if value in METRICS_SERIES_RESPONSE_RANGE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {METRICS_SERIES_RESPONSE_RANGE_VALUES!r}")
