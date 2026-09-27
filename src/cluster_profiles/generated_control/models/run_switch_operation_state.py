from typing import Literal, cast

RunSwitchOperationState = Literal['cancelled', 'failed', 'queued', 'running', 'succeeded', 'unknown', 'waiting', 'waiting-for-operator']

RUN_SWITCH_OPERATION_STATE_VALUES: set[RunSwitchOperationState] = { 'cancelled', 'failed', 'queued', 'running', 'succeeded', 'unknown', 'waiting', 'waiting-for-operator',  }

def check_run_switch_operation_state(value: str) -> RunSwitchOperationState:
    if value in RUN_SWITCH_OPERATION_STATE_VALUES:
        return cast(RunSwitchOperationState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_STATE_VALUES!r}")
