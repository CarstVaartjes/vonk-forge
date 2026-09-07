from typing import Literal, cast

RunSwitchCleanupResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_CLEANUP_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchCleanupResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_cleanup_result_subphase_type_0(value: str) -> RunSwitchCleanupResultSubphaseType0:
    if value in RUN_SWITCH_CLEANUP_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchCleanupResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CLEANUP_RESULT_SUBPHASE_TYPE_0_VALUES!r}")
