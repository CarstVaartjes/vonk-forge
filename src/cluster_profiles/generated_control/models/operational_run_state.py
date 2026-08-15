from typing import Literal, cast

OperationalRunState = Literal['failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping']

OPERATIONAL_RUN_STATE_VALUES: set[OperationalRunState] = { 'failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping',  }

def check_operational_run_state(value: str) -> OperationalRunState:
    if value in OPERATIONAL_RUN_STATE_VALUES:
        return cast(OperationalRunState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATIONAL_RUN_STATE_VALUES!r}")
