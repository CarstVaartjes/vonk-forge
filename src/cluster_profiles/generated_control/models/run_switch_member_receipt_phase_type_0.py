from typing import Literal, cast

RunSwitchMemberReceiptPhaseType0 = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_MEMBER_RECEIPT_PHASE_TYPE_0_VALUES: set[RunSwitchMemberReceiptPhaseType0] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_member_receipt_phase_type_0(value: str) -> RunSwitchMemberReceiptPhaseType0:
    if value in RUN_SWITCH_MEMBER_RECEIPT_PHASE_TYPE_0_VALUES:
        return cast(RunSwitchMemberReceiptPhaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_MEMBER_RECEIPT_PHASE_TYPE_0_VALUES!r}")
