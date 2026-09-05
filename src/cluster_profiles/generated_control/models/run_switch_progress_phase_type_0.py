from typing import Literal, cast

RunSwitchProgressPhaseType0 = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_PROGRESS_PHASE_TYPE_0_VALUES: set[RunSwitchProgressPhaseType0] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_progress_phase_type_0(value: str) -> RunSwitchProgressPhaseType0:
    if value in RUN_SWITCH_PROGRESS_PHASE_TYPE_0_VALUES:
        return cast(RunSwitchProgressPhaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PROGRESS_PHASE_TYPE_0_VALUES!r}")
