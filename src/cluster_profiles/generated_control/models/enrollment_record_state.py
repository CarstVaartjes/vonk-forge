from typing import Literal

EnrollmentRecordState = Literal['certificate_issued', 'ended', 'issuing']

ENROLLMENT_RECORD_STATE_VALUES: set[EnrollmentRecordState] = { 'certificate_issued', 'ended', 'issuing',  }

def check_enrollment_record_state(value: str) -> EnrollmentRecordState:
    if value in ENROLLMENT_RECORD_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_RECORD_STATE_VALUES!r}")
