from typing import Literal, cast

RunSwitchPhaseState = Literal['blocked', 'planned', 'retained', 'skipped']

RUN_SWITCH_PHASE_STATE_VALUES: set[RunSwitchPhaseState] = { 'blocked', 'planned', 'retained', 'skipped',  }

def check_run_switch_phase_state(value: str) -> RunSwitchPhaseState:
    if value in RUN_SWITCH_PHASE_STATE_VALUES:
        return cast(RunSwitchPhaseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PHASE_STATE_VALUES!r}")
