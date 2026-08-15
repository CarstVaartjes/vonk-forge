from typing import Literal, cast

RunPresenceGroupState = Literal['degraded', 'healthy']

RUN_PRESENCE_GROUP_STATE_VALUES: set[RunPresenceGroupState] = { 'degraded', 'healthy',  }

def check_run_presence_group_state(value: str) -> RunPresenceGroupState:
    if value in RUN_PRESENCE_GROUP_STATE_VALUES:
        return cast(RunPresenceGroupState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_PRESENCE_GROUP_STATE_VALUES!r}")
