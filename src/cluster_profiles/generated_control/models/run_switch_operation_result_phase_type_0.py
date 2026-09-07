from typing import Literal, cast

RunSwitchOperationResultPhaseType0 = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_OPERATION_RESULT_PHASE_TYPE_0_VALUES: set[RunSwitchOperationResultPhaseType0] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_operation_result_phase_type_0(value: str) -> RunSwitchOperationResultPhaseType0:
    if value in RUN_SWITCH_OPERATION_RESULT_PHASE_TYPE_0_VALUES:
        return cast(RunSwitchOperationResultPhaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_RESULT_PHASE_TYPE_0_VALUES!r}")
