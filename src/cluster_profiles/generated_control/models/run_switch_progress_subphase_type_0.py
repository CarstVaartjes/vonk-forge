from typing import Literal

RunSwitchProgressSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_PROGRESS_SUBPHASE_TYPE_0_VALUES: set[RunSwitchProgressSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_progress_subphase_type_0(value: str) -> RunSwitchProgressSubphaseType0:
    if value in RUN_SWITCH_PROGRESS_SUBPHASE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PROGRESS_SUBPHASE_TYPE_0_VALUES!r}")
