from typing import Literal

EnrollmentGrantState = Literal['consumed', 'expired', 'pending', 'revoked']

ENROLLMENT_GRANT_STATE_VALUES: set[EnrollmentGrantState] = { 'consumed', 'expired', 'pending', 'revoked',  }

def check_enrollment_grant_state(value: str) -> EnrollmentGrantState:
    if value in ENROLLMENT_GRANT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_GRANT_STATE_VALUES!r}")
