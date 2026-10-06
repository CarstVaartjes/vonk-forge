from typing import Literal

DistributionAssignmentState = Literal['active', 'expired', 'revoked']

DISTRIBUTION_ASSIGNMENT_STATE_VALUES: set[DistributionAssignmentState] = { 'active', 'expired', 'revoked',  }

def check_distribution_assignment_state(value: str) -> DistributionAssignmentState:
    if value in DISTRIBUTION_ASSIGNMENT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DISTRIBUTION_ASSIGNMENT_STATE_VALUES!r}")
