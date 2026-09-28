from typing import Literal

RunSwitchCleanupVerifyResultCleanupMode = Literal['reconcile', 'uninstall']

RUN_SWITCH_CLEANUP_VERIFY_RESULT_CLEANUP_MODE_VALUES: set[RunSwitchCleanupVerifyResultCleanupMode] = { 'reconcile', 'uninstall',  }

def check_run_switch_cleanup_verify_result_cleanup_mode(value: str) -> RunSwitchCleanupVerifyResultCleanupMode:
    if value in RUN_SWITCH_CLEANUP_VERIFY_RESULT_CLEANUP_MODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CLEANUP_VERIFY_RESULT_CLEANUP_MODE_VALUES!r}")
