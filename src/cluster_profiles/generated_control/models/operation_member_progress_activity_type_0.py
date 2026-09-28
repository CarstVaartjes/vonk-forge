from typing import Literal

OperationMemberProgressActivityType0 = Literal['active', 'possibly_stalled', 'waiting']

OPERATION_MEMBER_PROGRESS_ACTIVITY_TYPE_0_VALUES: set[OperationMemberProgressActivityType0] = { 'active', 'possibly_stalled', 'waiting',  }

def check_operation_member_progress_activity_type_0(value: str) -> OperationMemberProgressActivityType0:
    if value in OPERATION_MEMBER_PROGRESS_ACTIVITY_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATION_MEMBER_PROGRESS_ACTIVITY_TYPE_0_VALUES!r}")
