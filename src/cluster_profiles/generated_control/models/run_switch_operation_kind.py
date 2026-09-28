from typing import Literal

RunSwitchOperationKind = Literal['recipe.cleanup.v2', 'recipe.run-switch.v2', 'recipe.stop.v2']

RUN_SWITCH_OPERATION_KIND_VALUES: set[RunSwitchOperationKind] = { 'recipe.cleanup.v2', 'recipe.run-switch.v2', 'recipe.stop.v2',  }

def check_run_switch_operation_kind(value: str) -> RunSwitchOperationKind:
    if value in RUN_SWITCH_OPERATION_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_KIND_VALUES!r}")
