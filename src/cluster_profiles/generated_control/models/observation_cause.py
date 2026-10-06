from typing import Literal

ObservationCause = Literal['lease-lapsed', 'reported-unknown']

OBSERVATION_CAUSE_VALUES: set[ObservationCause] = { 'lease-lapsed', 'reported-unknown',  }

def check_observation_cause(value: str) -> ObservationCause:
    if value in OBSERVATION_CAUSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OBSERVATION_CAUSE_VALUES!r}")
