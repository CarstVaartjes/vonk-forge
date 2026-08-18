from typing import Literal, cast

EnrollmentDecisionResponseState = Literal['approved', 'certificate_issued', 'expired', 'pending_review', 'rejected']

ENROLLMENT_DECISION_RESPONSE_STATE_VALUES: set[EnrollmentDecisionResponseState] = { 'approved', 'certificate_issued', 'expired', 'pending_review', 'rejected',  }

def check_enrollment_decision_response_state(value: str) -> EnrollmentDecisionResponseState:
    if value in ENROLLMENT_DECISION_RESPONSE_STATE_VALUES:
        return cast(EnrollmentDecisionResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_DECISION_RESPONSE_STATE_VALUES!r}")
