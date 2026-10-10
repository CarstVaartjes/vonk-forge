from typing import Literal

GetMetricsSeriesMetric = Literal['certificate_expiry', 'gpu_memory_used', 'gpu_temperature', 'gpu_utilization', 'host_memory_used', 'request_rate']

GET_METRICS_SERIES_METRIC_VALUES: set[GetMetricsSeriesMetric] = { 'certificate_expiry', 'gpu_memory_used', 'gpu_temperature', 'gpu_utilization', 'host_memory_used', 'request_rate',  }

def check_get_metrics_series_metric(value: str) -> GetMetricsSeriesMetric:
    if value in GET_METRICS_SERIES_METRIC_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GET_METRICS_SERIES_METRIC_VALUES!r}")
