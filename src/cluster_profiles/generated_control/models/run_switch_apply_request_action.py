from typing import Literal, cast

RunSwitchApplyRequestAction = Literal['run', 'switch']

RUN_SWITCH_APPLY_REQUEST_ACTION_VALUES: set[RunSwitchApplyRequestAction] = { 'run', 'switch',  }

def check_run_switch_apply_request_action(value: str) -> RunSwitchApplyRequestAction:
    if value in RUN_SWITCH_APPLY_REQUEST_ACTION_VALUES:
        return cast(RunSwitchApplyRequestAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_APPLY_REQUEST_ACTION_VALUES!r}")
