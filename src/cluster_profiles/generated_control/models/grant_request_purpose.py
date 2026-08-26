from typing import Literal, cast

GrantRequestPurpose = Literal['new-node', 're-enroll']

GRANT_REQUEST_PURPOSE_VALUES: set[GrantRequestPurpose] = { 'new-node', 're-enroll',  }

def check_grant_request_purpose(value: str) -> GrantRequestPurpose:
    if value in GRANT_REQUEST_PURPOSE_VALUES:
        return cast(GrantRequestPurpose, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GRANT_REQUEST_PURPOSE_VALUES!r}")
