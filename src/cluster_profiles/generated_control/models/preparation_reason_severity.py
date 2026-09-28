from typing import Literal

PreparationReasonSeverity = Literal['blocker', 'info', 'warning']

PREPARATION_REASON_SEVERITY_VALUES: set[PreparationReasonSeverity] = { 'blocker', 'info', 'warning',  }

def check_preparation_reason_severity(value: str) -> PreparationReasonSeverity:
    if value in PREPARATION_REASON_SEVERITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PREPARATION_REASON_SEVERITY_VALUES!r}")
