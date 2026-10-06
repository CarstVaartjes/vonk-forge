from typing import Literal

RunSwitchProgressState = Literal['backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded', 'unknown']

RUN_SWITCH_PROGRESS_STATE_VALUES: set[RunSwitchProgressState] = { 'backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded', 'unknown',  }

def check_run_switch_progress_state(value: str) -> RunSwitchProgressState:
    if value in RUN_SWITCH_PROGRESS_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PROGRESS_STATE_VALUES!r}")
