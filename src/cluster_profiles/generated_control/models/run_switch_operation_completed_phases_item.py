from typing import Literal, cast

RunSwitchOperationCompletedPhasesItem = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_OPERATION_COMPLETED_PHASES_ITEM_VALUES: set[RunSwitchOperationCompletedPhasesItem] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_operation_completed_phases_item(value: str) -> RunSwitchOperationCompletedPhasesItem:
    if value in RUN_SWITCH_OPERATION_COMPLETED_PHASES_ITEM_VALUES:
        return cast(RunSwitchOperationCompletedPhasesItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_COMPLETED_PHASES_ITEM_VALUES!r}")
