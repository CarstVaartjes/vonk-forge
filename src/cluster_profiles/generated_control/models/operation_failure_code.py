from typing import Literal

OperationFailureCode = Literal['artifact_process_failed', 'fleet_profile_application_failed', 'stored_operation_result_unreadable']

OPERATION_FAILURE_CODE_VALUES: set[OperationFailureCode] = { 'artifact_process_failed', 'fleet_profile_application_failed', 'stored_operation_result_unreadable',  }

def check_operation_failure_code(value: str) -> OperationFailureCode:
    if value in OPERATION_FAILURE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATION_FAILURE_CODE_VALUES!r}")
