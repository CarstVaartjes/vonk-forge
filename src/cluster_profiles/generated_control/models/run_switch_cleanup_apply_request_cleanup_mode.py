from typing import Literal, cast

RunSwitchCleanupApplyRequestCleanupMode = Literal['reconcile', 'uninstall']

RUN_SWITCH_CLEANUP_APPLY_REQUEST_CLEANUP_MODE_VALUES: set[RunSwitchCleanupApplyRequestCleanupMode] = { 'reconcile', 'uninstall',  }

def check_run_switch_cleanup_apply_request_cleanup_mode(value: str) -> RunSwitchCleanupApplyRequestCleanupMode:
    if value in RUN_SWITCH_CLEANUP_APPLY_REQUEST_CLEANUP_MODE_VALUES:
        return cast(RunSwitchCleanupApplyRequestCleanupMode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CLEANUP_APPLY_REQUEST_CLEANUP_MODE_VALUES!r}")
