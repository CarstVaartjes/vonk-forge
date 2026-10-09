from typing import Literal

EnrollmentProfileState = Literal['ready']

ENROLLMENT_PROFILE_STATE_VALUES: set[EnrollmentProfileState] = { 'ready',  }

def check_enrollment_profile_state(value: str) -> EnrollmentProfileState:
    if value in ENROLLMENT_PROFILE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_PROFILE_STATE_VALUES!r}")
