from typing import Literal

ProjectionReasonCode = Literal['cpu.low-clock', 'install.partial', 'inventory.missing', 'inventory.stale', 'node.offline', 'run.degraded', 'telemetry.delayed', 'telemetry.missing', 'telemetry.stale']

PROJECTION_REASON_CODE_VALUES: set[ProjectionReasonCode] = { 'cpu.low-clock', 'install.partial', 'inventory.missing', 'inventory.stale', 'node.offline', 'run.degraded', 'telemetry.delayed', 'telemetry.missing', 'telemetry.stale',  }

def check_projection_reason_code(value: str) -> ProjectionReasonCode:
    if value in PROJECTION_REASON_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROJECTION_REASON_CODE_VALUES!r}")
