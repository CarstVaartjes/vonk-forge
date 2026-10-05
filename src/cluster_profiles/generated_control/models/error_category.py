from typing import Literal

ErrorCategory = Literal['invalid-request', 'security-refusal', 'unknown']

ERROR_CATEGORY_VALUES: set[ErrorCategory] = { 'invalid-request', 'security-refusal', 'unknown',  }

def check_error_category(value: str) -> ErrorCategory:
    if value in ERROR_CATEGORY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ERROR_CATEGORY_VALUES!r}")
