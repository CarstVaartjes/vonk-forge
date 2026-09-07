from typing import Literal, cast

RunSwitchStopResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_STOP_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchStopResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_stop_result_subphase_type_0(value: str) -> RunSwitchStopResultSubphaseType0:
    if value in RUN_SWITCH_STOP_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchStopResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_STOP_RESULT_SUBPHASE_TYPE_0_VALUES!r}")
