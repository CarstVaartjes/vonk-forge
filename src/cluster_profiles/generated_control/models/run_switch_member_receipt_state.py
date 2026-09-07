from typing import Literal, cast

RunSwitchMemberReceiptState = Literal['failed', 'pending', 'running', 'succeeded', 'unknown']

RUN_SWITCH_MEMBER_RECEIPT_STATE_VALUES: set[RunSwitchMemberReceiptState] = { 'failed', 'pending', 'running', 'succeeded', 'unknown',  }

def check_run_switch_member_receipt_state(value: str) -> RunSwitchMemberReceiptState:
    if value in RUN_SWITCH_MEMBER_RECEIPT_STATE_VALUES:
        return cast(RunSwitchMemberReceiptState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_MEMBER_RECEIPT_STATE_VALUES!r}")
