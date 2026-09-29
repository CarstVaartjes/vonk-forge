from typing import Literal

FleetProfilePlanStepKind = Literal['prepare', 'switch']

FLEET_PROFILE_PLAN_STEP_KIND_VALUES: set[FleetProfilePlanStepKind] = { 'prepare', 'switch',  }

def check_fleet_profile_plan_step_kind(value: str) -> FleetProfilePlanStepKind:
    if value in FLEET_PROFILE_PLAN_STEP_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_PLAN_STEP_KIND_VALUES!r}")
