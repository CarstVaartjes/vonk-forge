from typing import Literal, cast

RunSwitchOperationCleanupModeType0 = Literal['reconcile', 'uninstall']

RUN_SWITCH_OPERATION_CLEANUP_MODE_TYPE_0_VALUES: set[RunSwitchOperationCleanupModeType0] = { 'reconcile', 'uninstall',  }

def check_run_switch_operation_cleanup_mode_type_0(value: str) -> RunSwitchOperationCleanupModeType0:
    if value in RUN_SWITCH_OPERATION_CLEANUP_MODE_TYPE_0_VALUES:
        return cast(RunSwitchOperationCleanupModeType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_CLEANUP_MODE_TYPE_0_VALUES!r}")
