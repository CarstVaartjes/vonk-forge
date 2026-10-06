from typing import Literal

StopPlanCode = Literal['stop.capacity_release_deferred', 'stop.rank_membership_changed', 'stop.reservation_membership_changed', 'stop.run_not_stoppable', 'stop.target_scope_changed']

STOP_PLAN_CODE_VALUES: set[StopPlanCode] = { 'stop.capacity_release_deferred', 'stop.rank_membership_changed', 'stop.reservation_membership_changed', 'stop.run_not_stoppable', 'stop.target_scope_changed',  }

def check_stop_plan_code(value: str) -> StopPlanCode:
    if value in STOP_PLAN_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STOP_PLAN_CODE_VALUES!r}")
