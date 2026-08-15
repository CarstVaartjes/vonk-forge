from typing import Literal, cast

ProjectionReasonCode = Literal['install.partial', 'inventory.missing', 'inventory.stale', 'node.offline', 'run.degraded', 'telemetry.delayed', 'telemetry.missing', 'telemetry.stale']

PROJECTION_REASON_CODE_VALUES: set[ProjectionReasonCode] = { 'install.partial', 'inventory.missing', 'inventory.stale', 'node.offline', 'run.degraded', 'telemetry.delayed', 'telemetry.missing', 'telemetry.stale',  }

def check_projection_reason_code(value: str) -> ProjectionReasonCode:
    if value in PROJECTION_REASON_CODE_VALUES:
        return cast(ProjectionReasonCode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROJECTION_REASON_CODE_VALUES!r}")
