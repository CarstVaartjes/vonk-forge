from typing import Literal, cast

RunPresenceRouteState = Literal['failed', 'pending', 'published', 'withdrawn']

RUN_PRESENCE_ROUTE_STATE_VALUES: set[RunPresenceRouteState] = { 'failed', 'pending', 'published', 'withdrawn',  }

def check_run_presence_route_state(value: str) -> RunPresenceRouteState:
    if value in RUN_PRESENCE_ROUTE_STATE_VALUES:
        return cast(RunPresenceRouteState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_PRESENCE_ROUTE_STATE_VALUES!r}")
