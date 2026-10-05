from typing import Literal

OperatorActionName = Literal['resume', 'retire', 'retry', 'stop']

OPERATOR_ACTION_NAME_VALUES: set[OperatorActionName] = { 'resume', 'retire', 'retry', 'stop',  }

def check_operator_action_name(value: str) -> OperatorActionName:
    if value in OPERATOR_ACTION_NAME_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATOR_ACTION_NAME_VALUES!r}")
