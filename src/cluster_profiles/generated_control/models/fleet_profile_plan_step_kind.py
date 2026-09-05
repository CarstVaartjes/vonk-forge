from typing import Literal, cast

FleetProfilePlanStepKind = Literal['build', 'create-placement', 'distribute-image', 'install', 'start', 'stop', 'switch', 'uninstall']

FLEET_PROFILE_PLAN_STEP_KIND_VALUES: set[FleetProfilePlanStepKind] = { 'build', 'create-placement', 'distribute-image', 'install', 'start', 'stop', 'switch', 'uninstall',  }

def check_fleet_profile_plan_step_kind(value: str) -> FleetProfilePlanStepKind:
    if value in FLEET_PROFILE_PLAN_STEP_KIND_VALUES:
        return cast(FleetProfilePlanStepKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_PLAN_STEP_KIND_VALUES!r}")
