from typing import Literal, cast

EnrollmentGrantStatusState = Literal['consumed', 'expired', 'pending', 'revoked']

ENROLLMENT_GRANT_STATUS_STATE_VALUES: set[EnrollmentGrantStatusState] = { 'consumed', 'expired', 'pending', 'revoked',  }

def check_enrollment_grant_status_state(value: str) -> EnrollmentGrantStatusState:
    if value in ENROLLMENT_GRANT_STATUS_STATE_VALUES:
        return cast(EnrollmentGrantStatusState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_GRANT_STATUS_STATE_VALUES!r}")
