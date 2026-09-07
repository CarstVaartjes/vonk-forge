from typing import Literal, cast

RunSwitchPlanAction = Literal['run', 'stop', 'switch']

RUN_SWITCH_PLAN_ACTION_VALUES: set[RunSwitchPlanAction] = { 'run', 'stop', 'switch',  }

def check_run_switch_plan_action(value: str) -> RunSwitchPlanAction:
    if value in RUN_SWITCH_PLAN_ACTION_VALUES:
        return cast(RunSwitchPlanAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PLAN_ACTION_VALUES!r}")
