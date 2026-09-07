from typing import Literal, cast

RunSwitchFinalVerifyResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_FINAL_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchFinalVerifyResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_final_verify_result_subphase_type_0(value: str) -> RunSwitchFinalVerifyResultSubphaseType0:
    if value in RUN_SWITCH_FINAL_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchFinalVerifyResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_FINAL_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES!r}")
