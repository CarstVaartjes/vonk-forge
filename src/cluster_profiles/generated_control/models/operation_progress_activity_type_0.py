from typing import Literal

OperationProgressActivityType0 = Literal['active', 'possibly_stalled', 'waiting']

OPERATION_PROGRESS_ACTIVITY_TYPE_0_VALUES: set[OperationProgressActivityType0] = { 'active', 'possibly_stalled', 'waiting',  }

def check_operation_progress_activity_type_0(value: str) -> OperationProgressActivityType0:
    if value in OPERATION_PROGRESS_ACTIVITY_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATION_PROGRESS_ACTIVITY_TYPE_0_VALUES!r}")
