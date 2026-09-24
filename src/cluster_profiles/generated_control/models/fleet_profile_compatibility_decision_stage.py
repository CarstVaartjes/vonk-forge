from typing import Literal, cast

FleetProfileCompatibilityDecisionStage = Literal['controller-prepare', 'target-prepare']

FLEET_PROFILE_COMPATIBILITY_DECISION_STAGE_VALUES: set[FleetProfileCompatibilityDecisionStage] = { 'controller-prepare', 'target-prepare',  }

def check_fleet_profile_compatibility_decision_stage(value: str) -> FleetProfileCompatibilityDecisionStage:
    if value in FLEET_PROFILE_COMPATIBILITY_DECISION_STAGE_VALUES:
        return cast(FleetProfileCompatibilityDecisionStage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_COMPATIBILITY_DECISION_STAGE_VALUES!r}")
