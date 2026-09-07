from typing import Literal, cast

RunSwitchOperationResultCompletedPhasesItem = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify']

RUN_SWITCH_OPERATION_RESULT_COMPLETED_PHASES_ITEM_VALUES: set[RunSwitchOperationResultCompletedPhasesItem] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'verify',  }

def check_run_switch_operation_result_completed_phases_item(value: str) -> RunSwitchOperationResultCompletedPhasesItem:
    if value in RUN_SWITCH_OPERATION_RESULT_COMPLETED_PHASES_ITEM_VALUES:
        return cast(RunSwitchOperationResultCompletedPhasesItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_RESULT_COMPLETED_PHASES_ITEM_VALUES!r}")
