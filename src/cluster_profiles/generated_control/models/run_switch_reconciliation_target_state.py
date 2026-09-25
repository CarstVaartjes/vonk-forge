from typing import Literal, cast

RunSwitchReconciliationTargetState = Literal['pending', 'reconciled']

RUN_SWITCH_RECONCILIATION_TARGET_STATE_VALUES: set[RunSwitchReconciliationTargetState] = { 'pending', 'reconciled',  }

def check_run_switch_reconciliation_target_state(value: str) -> RunSwitchReconciliationTargetState:
    if value in RUN_SWITCH_RECONCILIATION_TARGET_STATE_VALUES:
        return cast(RunSwitchReconciliationTargetState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_RECONCILIATION_TARGET_STATE_VALUES!r}")
