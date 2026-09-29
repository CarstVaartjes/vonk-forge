from typing import Literal

OperationBlockerSeverity = Literal['error', 'info', 'warning']

OPERATION_BLOCKER_SEVERITY_VALUES: set[OperationBlockerSeverity] = { 'error', 'info', 'warning',  }

def check_operation_blocker_severity(value: str) -> OperationBlockerSeverity:
    if value in OPERATION_BLOCKER_SEVERITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATION_BLOCKER_SEVERITY_VALUES!r}")
