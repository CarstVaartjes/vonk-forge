from typing import Literal, cast

RunSwitchStartResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_START_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchStartResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_start_result_subphase_type_0(value: str) -> RunSwitchStartResultSubphaseType0:
    if value in RUN_SWITCH_START_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchStartResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_START_RESULT_SUBPHASE_TYPE_0_VALUES!r}")
