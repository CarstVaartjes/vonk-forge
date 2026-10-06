from typing import Literal

ObservedAssignmentState = Literal['degraded', 'installed', 'installing', 'not-placed', 'placed', 'running']

OBSERVED_ASSIGNMENT_STATE_VALUES: set[ObservedAssignmentState] = { 'degraded', 'installed', 'installing', 'not-placed', 'placed', 'running',  }

def check_observed_assignment_state(value: str) -> ObservedAssignmentState:
    if value in OBSERVED_ASSIGNMENT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OBSERVED_ASSIGNMENT_STATE_VALUES!r}")
