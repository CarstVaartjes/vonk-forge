from typing import Literal, cast

OperationalRunRouteState = Literal['failed', 'pending', 'published', 'withdrawn']

OPERATIONAL_RUN_ROUTE_STATE_VALUES: set[OperationalRunRouteState] = { 'failed', 'pending', 'published', 'withdrawn',  }

def check_operational_run_route_state(value: str) -> OperationalRunRouteState:
    if value in OPERATIONAL_RUN_ROUTE_STATE_VALUES:
        return cast(OperationalRunRouteState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATIONAL_RUN_ROUTE_STATE_VALUES!r}")
