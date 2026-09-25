from typing import Literal, cast

RunSwitchPlanCleanupDisposition = Literal['abandon', 'uninstall']

RUN_SWITCH_PLAN_CLEANUP_DISPOSITION_VALUES: set[RunSwitchPlanCleanupDisposition] = { 'abandon', 'uninstall',  }

def check_run_switch_plan_cleanup_disposition(value: str) -> RunSwitchPlanCleanupDisposition:
    if value in RUN_SWITCH_PLAN_CLEANUP_DISPOSITION_VALUES:
        return cast(RunSwitchPlanCleanupDisposition, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_PLAN_CLEANUP_DISPOSITION_VALUES!r}")
