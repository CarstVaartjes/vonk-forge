from typing import Literal

InvalidRequestReason = Literal['conflict', 'duplicate', 'immutable', 'incomplete', 'limit-exceeded', 'malformed', 'not-found', 'not-ready', 'out-of-range', 'superseded', 'unknown-field', 'unsupported']

INVALID_REQUEST_REASON_VALUES: set[InvalidRequestReason] = { 'conflict', 'duplicate', 'immutable', 'incomplete', 'limit-exceeded', 'malformed', 'not-found', 'not-ready', 'out-of-range', 'superseded', 'unknown-field', 'unsupported',  }

def check_invalid_request_reason(value: str) -> InvalidRequestReason:
    if value in INVALID_REQUEST_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INVALID_REQUEST_REASON_VALUES!r}")
