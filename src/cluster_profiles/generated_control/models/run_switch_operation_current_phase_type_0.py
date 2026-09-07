from typing import Literal, cast

RunSwitchOperationCurrentPhaseType0 = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_OPERATION_CURRENT_PHASE_TYPE_0_VALUES: set[RunSwitchOperationCurrentPhaseType0] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_operation_current_phase_type_0(value: str) -> RunSwitchOperationCurrentPhaseType0:
    if value in RUN_SWITCH_OPERATION_CURRENT_PHASE_TYPE_0_VALUES:
        return cast(RunSwitchOperationCurrentPhaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_CURRENT_PHASE_TYPE_0_VALUES!r}")
