from typing import Literal, cast

RunSwitchOperationAction = Literal['run', 'stop', 'switch']

RUN_SWITCH_OPERATION_ACTION_VALUES: set[RunSwitchOperationAction] = { 'run', 'stop', 'switch',  }

def check_run_switch_operation_action(value: str) -> RunSwitchOperationAction:
    if value in RUN_SWITCH_OPERATION_ACTION_VALUES:
        return cast(RunSwitchOperationAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_ACTION_VALUES!r}")
