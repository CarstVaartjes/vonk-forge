from typing import Literal, cast

RunSwitchPhaseKind = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_PHASE_KIND_VALUES: set[RunSwitchPhaseKind] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_phase_kind(value: str) -> RunSwitchPhaseKind:
    if value in RUN_SWITCH_PHASE_KIND_VALUES:
        return cast(RunSwitchPhaseKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PHASE_KIND_VALUES!r}")
