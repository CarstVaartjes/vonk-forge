from typing import Literal, cast

RunSwitchPhaseSubphaseType0 = Literal['container-build', 'model-download', 'runtime-install', 'target-copy']

RUN_SWITCH_PHASE_SUBPHASE_TYPE_0_VALUES: set[RunSwitchPhaseSubphaseType0] = { 'container-build', 'model-download', 'runtime-install', 'target-copy',  }

def check_run_switch_phase_subphase_type_0(value: str) -> RunSwitchPhaseSubphaseType0:
    if value in RUN_SWITCH_PHASE_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchPhaseSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PHASE_SUBPHASE_TYPE_0_VALUES!r}")
