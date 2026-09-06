from typing import Literal, cast

TelemetryCapabilityScope = Literal['accelerator', 'benchmark', 'memory', 'network', 'node', 'runtime', 'service', 'storage', 'workload']

TELEMETRY_CAPABILITY_SCOPE_VALUES: set[TelemetryCapabilityScope] = { 'accelerator', 'benchmark', 'memory', 'network', 'node', 'runtime', 'service', 'storage', 'workload',  }

def check_telemetry_capability_scope(value: str) -> TelemetryCapabilityScope:
    if value in TELEMETRY_CAPABILITY_SCOPE_VALUES:
        return cast(TelemetryCapabilityScope, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_CAPABILITY_SCOPE_VALUES!r}")
