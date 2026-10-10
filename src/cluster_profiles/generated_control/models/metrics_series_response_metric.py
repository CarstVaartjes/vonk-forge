from typing import Literal

MetricsSeriesResponseMetric = Literal['certificate_expiry', 'gpu_memory_used', 'gpu_temperature', 'gpu_utilization', 'host_memory_used', 'request_rate']

METRICS_SERIES_RESPONSE_METRIC_VALUES: set[MetricsSeriesResponseMetric] = { 'certificate_expiry', 'gpu_memory_used', 'gpu_temperature', 'gpu_utilization', 'host_memory_used', 'request_rate',  }

def check_metrics_series_response_metric(value: str) -> MetricsSeriesResponseMetric:
    if value in METRICS_SERIES_RESPONSE_METRIC_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {METRICS_SERIES_RESPONSE_METRIC_VALUES!r}")
