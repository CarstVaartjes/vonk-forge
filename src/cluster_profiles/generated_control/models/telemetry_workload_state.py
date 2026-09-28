from typing import Literal

TelemetryWorkloadState = Literal['cancelled', 'completed', 'failed', 'queued', 'running', 'unknown']

TELEMETRY_WORKLOAD_STATE_VALUES: set[TelemetryWorkloadState] = { 'cancelled', 'completed', 'failed', 'queued', 'running', 'unknown',  }

def check_telemetry_workload_state(value: str) -> TelemetryWorkloadState:
    if value in TELEMETRY_WORKLOAD_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_WORKLOAD_STATE_VALUES!r}")
