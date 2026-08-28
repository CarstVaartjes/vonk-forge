from typing import Literal, cast

FleetProfilePlanStepKind = Literal['create-placement', 'distribute-image', 'install', 'start', 'stop', 'uninstall']

FLEET_PROFILE_PLAN_STEP_KIND_VALUES: set[FleetProfilePlanStepKind] = { 'create-placement', 'distribute-image', 'install', 'start', 'stop', 'uninstall',  }

def check_fleet_profile_plan_step_kind(value: str) -> FleetProfilePlanStepKind:
    if value in FLEET_PROFILE_PLAN_STEP_KIND_VALUES:
        return cast(FleetProfilePlanStepKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_PLAN_STEP_KIND_VALUES!r}")
