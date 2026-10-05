from typing import Literal

LifecycleState = Literal['backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded']

LIFECYCLE_STATE_VALUES: set[LifecycleState] = { 'backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded',  }

def check_lifecycle_state(value: str) -> LifecycleState:
    if value in LIFECYCLE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIFECYCLE_STATE_VALUES!r}")
