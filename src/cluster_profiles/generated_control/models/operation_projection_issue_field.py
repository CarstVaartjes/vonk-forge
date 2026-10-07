from typing import Literal

OperationProjectionIssueField = Literal['cancellation', 'progress']

OPERATION_PROJECTION_ISSUE_FIELD_VALUES: set[OperationProjectionIssueField] = { 'cancellation', 'progress',  }

def check_operation_projection_issue_field(value: str) -> OperationProjectionIssueField:
    if value in OPERATION_PROJECTION_ISSUE_FIELD_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATION_PROJECTION_ISSUE_FIELD_VALUES!r}")
