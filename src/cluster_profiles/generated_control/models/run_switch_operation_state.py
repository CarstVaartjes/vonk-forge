from typing import Literal

RunSwitchOperationState = Literal['backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded', 'unknown']

RUN_SWITCH_OPERATION_STATE_VALUES: set[RunSwitchOperationState] = { 'backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded', 'unknown',  }

def check_run_switch_operation_state(value: str) -> RunSwitchOperationState:
    if value in RUN_SWITCH_OPERATION_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_STATE_VALUES!r}")
