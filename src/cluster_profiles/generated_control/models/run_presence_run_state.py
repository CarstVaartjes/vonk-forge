from typing import Literal, cast

RunPresenceRunState = Literal['failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping']

RUN_PRESENCE_RUN_STATE_VALUES: set[RunPresenceRunState] = { 'failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping',  }

def check_run_presence_run_state(value: str) -> RunPresenceRunState:
    if value in RUN_PRESENCE_RUN_STATE_VALUES:
        return cast(RunPresenceRunState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_PRESENCE_RUN_STATE_VALUES!r}")
