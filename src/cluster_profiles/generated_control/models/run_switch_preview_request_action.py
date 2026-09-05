from typing import Literal, cast

RunSwitchPreviewRequestAction = Literal['run', 'switch']

RUN_SWITCH_PREVIEW_REQUEST_ACTION_VALUES: set[RunSwitchPreviewRequestAction] = { 'run', 'switch',  }

def check_run_switch_preview_request_action(value: str) -> RunSwitchPreviewRequestAction:
    if value in RUN_SWITCH_PREVIEW_REQUEST_ACTION_VALUES:
        return cast(RunSwitchPreviewRequestAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PREVIEW_REQUEST_ACTION_VALUES!r}")
