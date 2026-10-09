from typing import Literal

EnrollmentPurpose = Literal['new-node', 're-enroll']

ENROLLMENT_PURPOSE_VALUES: set[EnrollmentPurpose] = { 'new-node', 're-enroll',  }

def check_enrollment_purpose(value: str) -> EnrollmentPurpose:
    if value in ENROLLMENT_PURPOSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_PURPOSE_VALUES!r}")
