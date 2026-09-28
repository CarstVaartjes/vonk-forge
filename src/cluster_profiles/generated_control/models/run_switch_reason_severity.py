from typing import Literal

RunSwitchReasonSeverity = Literal['blocker', 'info', 'warning']

RUN_SWITCH_REASON_SEVERITY_VALUES: set[RunSwitchReasonSeverity] = { 'blocker', 'info', 'warning',  }

def check_run_switch_reason_severity(value: str) -> RunSwitchReasonSeverity:
    if value in RUN_SWITCH_REASON_SEVERITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_REASON_SEVERITY_VALUES!r}")
