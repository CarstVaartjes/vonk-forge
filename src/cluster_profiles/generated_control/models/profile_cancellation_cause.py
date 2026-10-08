from typing import Literal

ProfileCancellationCause = Literal['operator', 'superseded']

PROFILE_CANCELLATION_CAUSE_VALUES: set[ProfileCancellationCause] = { 'operator', 'superseded',  }

def check_profile_cancellation_cause(value: str) -> ProfileCancellationCause:
    if value in PROFILE_CANCELLATION_CAUSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_CANCELLATION_CAUSE_VALUES!r}")
