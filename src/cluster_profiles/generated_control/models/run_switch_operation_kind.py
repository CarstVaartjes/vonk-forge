from typing import Literal, cast

RunSwitchOperationKind = Literal['recipe.run-switch.v2', 'recipe.stop.v2']

RUN_SWITCH_OPERATION_KIND_VALUES: set[RunSwitchOperationKind] = { 'recipe.run-switch.v2', 'recipe.stop.v2',  }

def check_run_switch_operation_kind(value: str) -> RunSwitchOperationKind:
    if value in RUN_SWITCH_OPERATION_KIND_VALUES:
        return cast(RunSwitchOperationKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_OPERATION_KIND_VALUES!r}")
