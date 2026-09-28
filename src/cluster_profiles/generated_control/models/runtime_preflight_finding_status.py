from typing import Literal

RuntimePreflightFindingStatus = Literal['failed', 'passed', 'unknown']

RUNTIME_PREFLIGHT_FINDING_STATUS_VALUES: set[RuntimePreflightFindingStatus] = { 'failed', 'passed', 'unknown',  }

def check_runtime_preflight_finding_status(value: str) -> RuntimePreflightFindingStatus:
    if value in RUNTIME_PREFLIGHT_FINDING_STATUS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_PREFLIGHT_FINDING_STATUS_VALUES!r}")
