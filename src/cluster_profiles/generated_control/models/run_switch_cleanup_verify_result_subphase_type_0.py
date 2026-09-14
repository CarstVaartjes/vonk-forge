from typing import Literal, cast

RunSwitchCleanupVerifyResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_CLEANUP_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchCleanupVerifyResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_cleanup_verify_result_subphase_type_0(value: str) -> RunSwitchCleanupVerifyResultSubphaseType0:
    if value in RUN_SWITCH_CLEANUP_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchCleanupVerifyResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CLEANUP_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES!r}")
