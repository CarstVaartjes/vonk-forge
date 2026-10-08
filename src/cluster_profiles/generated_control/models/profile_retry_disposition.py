from typing import Literal

ProfileRetryDisposition = Literal['supersede', 'wait']

PROFILE_RETRY_DISPOSITION_VALUES: set[ProfileRetryDisposition] = { 'supersede', 'wait',  }

def check_profile_retry_disposition(value: str) -> ProfileRetryDisposition:
    if value in PROFILE_RETRY_DISPOSITION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_RETRY_DISPOSITION_VALUES!r}")
