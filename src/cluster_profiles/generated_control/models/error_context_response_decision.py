from typing import Literal, cast

ErrorContextResponseDecision = Literal['defer', 'exit', 'retry']

ERROR_CONTEXT_RESPONSE_DECISION_VALUES: set[ErrorContextResponseDecision] = { 'defer', 'exit', 'retry',  }

def check_error_context_response_decision(value: str) -> ErrorContextResponseDecision:
    if value in ERROR_CONTEXT_RESPONSE_DECISION_VALUES:
        return cast(ErrorContextResponseDecision, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ERROR_CONTEXT_RESPONSE_DECISION_VALUES!r}")
