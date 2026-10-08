from typing import Literal

ProfileReasonSeverity = Literal['blocker', 'error', 'info', 'warning']

PROFILE_REASON_SEVERITY_VALUES: set[ProfileReasonSeverity] = { 'blocker', 'error', 'info', 'warning',  }

def check_profile_reason_severity(value: str) -> ProfileReasonSeverity:
    if value in PROFILE_REASON_SEVERITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_REASON_SEVERITY_VALUES!r}")
