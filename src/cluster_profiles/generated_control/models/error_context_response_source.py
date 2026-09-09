from typing import Literal, cast

ErrorContextResponseSource = Literal['local_io', 'protocol', 'remote_rejection', 'transport', 'unknown']

ERROR_CONTEXT_RESPONSE_SOURCE_VALUES: set[ErrorContextResponseSource] = { 'local_io', 'protocol', 'remote_rejection', 'transport', 'unknown',  }

def check_error_context_response_source(value: str) -> ErrorContextResponseSource:
    if value in ERROR_CONTEXT_RESPONSE_SOURCE_VALUES:
        return cast(ErrorContextResponseSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ERROR_CONTEXT_RESPONSE_SOURCE_VALUES!r}")
