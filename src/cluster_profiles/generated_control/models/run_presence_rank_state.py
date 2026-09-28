from typing import Literal

RunPresenceRankState = Literal['failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping']

RUN_PRESENCE_RANK_STATE_VALUES: set[RunPresenceRankState] = { 'failed', 'lost', 'planned', 'running', 'starting', 'stopped', 'stopping',  }

def check_run_presence_rank_state(value: str) -> RunPresenceRankState:
    if value in RUN_PRESENCE_RANK_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_PRESENCE_RANK_STATE_VALUES!r}")
