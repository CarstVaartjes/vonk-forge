from typing import Literal

ProjectionReasonSeverity = Literal['error', 'info', 'warning']

PROJECTION_REASON_SEVERITY_VALUES: set[ProjectionReasonSeverity] = { 'error', 'info', 'warning',  }

def check_projection_reason_severity(value: str) -> ProjectionReasonSeverity:
    if value in PROJECTION_REASON_SEVERITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROJECTION_REASON_SEVERITY_VALUES!r}")
