from typing import Literal

EndpointState = Literal['expired', 'installed-only', 'not-published-yet', 'published', 'unavailable', 'withdrawn']

ENDPOINT_STATE_VALUES: set[EndpointState] = { 'expired', 'installed-only', 'not-published-yet', 'published', 'unavailable', 'withdrawn',  }

def check_endpoint_state(value: str) -> EndpointState:
    if value in ENDPOINT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENDPOINT_STATE_VALUES!r}")
