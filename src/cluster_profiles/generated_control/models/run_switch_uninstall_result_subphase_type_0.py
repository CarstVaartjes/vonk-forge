from typing import Literal, cast

RunSwitchUninstallResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_UNINSTALL_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchUninstallResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_uninstall_result_subphase_type_0(value: str) -> RunSwitchUninstallResultSubphaseType0:
    if value in RUN_SWITCH_UNINSTALL_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchUninstallResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_UNINSTALL_RESULT_SUBPHASE_TYPE_0_VALUES!r}")
