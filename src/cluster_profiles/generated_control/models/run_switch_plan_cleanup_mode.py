from typing import Literal

RunSwitchPlanCleanupMode = Literal['reconcile', 'uninstall']

RUN_SWITCH_PLAN_CLEANUP_MODE_VALUES: set[RunSwitchPlanCleanupMode] = { 'reconcile', 'uninstall',  }

def check_run_switch_plan_cleanup_mode(value: str) -> RunSwitchPlanCleanupMode:
    if value in RUN_SWITCH_PLAN_CLEANUP_MODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PLAN_CLEANUP_MODE_VALUES!r}")
