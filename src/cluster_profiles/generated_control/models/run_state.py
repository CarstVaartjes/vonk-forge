from typing import Literal

RunState = Literal['failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping']

RUN_STATE_VALUES: set[RunState] = { 'failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping',  }

def check_run_state(value: str) -> RunState:
    if value in RUN_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_STATE_VALUES!r}")
