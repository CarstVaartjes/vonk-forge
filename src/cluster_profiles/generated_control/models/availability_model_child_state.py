from typing import Literal

AvailabilityModelChildState = Literal['backoff', 'cancelled', 'failed', 'observing', 'queued', 'running', 'succeeded']

AVAILABILITY_MODEL_CHILD_STATE_VALUES: set[AvailabilityModelChildState] = { 'backoff', 'cancelled', 'failed', 'observing', 'queued', 'running', 'succeeded',  }

def check_availability_model_child_state(value: str) -> AvailabilityModelChildState:
    if value in AVAILABILITY_MODEL_CHILD_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AVAILABILITY_MODEL_CHILD_STATE_VALUES!r}")
