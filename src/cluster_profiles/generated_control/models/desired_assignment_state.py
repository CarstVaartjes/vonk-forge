from typing import Literal

DesiredAssignmentState = Literal['installed', 'running']

DESIRED_ASSIGNMENT_STATE_VALUES: set[DesiredAssignmentState] = { 'installed', 'running',  }

def check_desired_assignment_state(value: str) -> DesiredAssignmentState:
    if value in DESIRED_ASSIGNMENT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DESIRED_ASSIGNMENT_STATE_VALUES!r}")
