from typing import Literal, cast

TelemetryRuntimeReadiness = Literal['failed', 'queued', 'ready', 'running', 'starting', 'stopped', 'unknown']

TELEMETRY_RUNTIME_READINESS_VALUES: set[TelemetryRuntimeReadiness] = { 'failed', 'queued', 'ready', 'running', 'starting', 'stopped', 'unknown',  }

def check_telemetry_runtime_readiness(value: str) -> TelemetryRuntimeReadiness:
    if value in TELEMETRY_RUNTIME_READINESS_VALUES:
        return cast(TelemetryRuntimeReadiness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_RUNTIME_READINESS_VALUES!r}")
