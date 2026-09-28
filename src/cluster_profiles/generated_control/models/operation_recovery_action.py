from typing import Literal

OperationRecoveryAction = Literal['cancel', 'inspect', 'resume', 'retry']

OPERATION_RECOVERY_ACTION_VALUES: set[OperationRecoveryAction] = { 'cancel', 'inspect', 'resume', 'retry',  }

def check_operation_recovery_action(value: str) -> OperationRecoveryAction:
    if value in OPERATION_RECOVERY_ACTION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATION_RECOVERY_ACTION_VALUES!r}")
