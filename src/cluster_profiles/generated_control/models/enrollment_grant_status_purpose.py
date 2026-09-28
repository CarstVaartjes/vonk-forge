from typing import Literal

EnrollmentGrantStatusPurpose = Literal['new-node', 're-enroll']

ENROLLMENT_GRANT_STATUS_PURPOSE_VALUES: set[EnrollmentGrantStatusPurpose] = { 'new-node', 're-enroll',  }

def check_enrollment_grant_status_purpose(value: str) -> EnrollmentGrantStatusPurpose:
    if value in ENROLLMENT_GRANT_STATUS_PURPOSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_GRANT_STATUS_PURPOSE_VALUES!r}")
