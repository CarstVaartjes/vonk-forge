from typing import Literal, cast

TelemetrySeriesScope = Literal['accelerator', 'benchmark', 'memory', 'network', 'node', 'runtime', 'service', 'storage', 'workload']

TELEMETRY_SERIES_SCOPE_VALUES: set[TelemetrySeriesScope] = { 'accelerator', 'benchmark', 'memory', 'network', 'node', 'runtime', 'service', 'storage', 'workload',  }

def check_telemetry_series_scope(value: str) -> TelemetrySeriesScope:
    if value in TELEMETRY_SERIES_SCOPE_VALUES:
        return cast(TelemetrySeriesScope, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_SERIES_SCOPE_VALUES!r}")
