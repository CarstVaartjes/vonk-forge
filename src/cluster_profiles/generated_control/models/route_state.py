from typing import Literal

RouteState = Literal['failed', 'pending', 'published', 'withdrawn']

ROUTE_STATE_VALUES: set[RouteState] = { 'failed', 'pending', 'published', 'withdrawn',  }

def check_route_state(value: str) -> RouteState:
    if value in ROUTE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ROUTE_STATE_VALUES!r}")
