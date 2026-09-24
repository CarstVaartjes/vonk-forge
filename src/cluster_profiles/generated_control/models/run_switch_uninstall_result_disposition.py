from typing import Literal, cast

RunSwitchUninstallResultDisposition = Literal['abandoned', 'uninstalled']

RUN_SWITCH_UNINSTALL_RESULT_DISPOSITION_VALUES: set[RunSwitchUninstallResultDisposition] = { 'abandoned', 'uninstalled',  }

def check_run_switch_uninstall_result_disposition(value: str) -> RunSwitchUninstallResultDisposition:
    if value in RUN_SWITCH_UNINSTALL_RESULT_DISPOSITION_VALUES:
        return cast(RunSwitchUninstallResultDisposition, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_UNINSTALL_RESULT_DISPOSITION_VALUES!r}")
