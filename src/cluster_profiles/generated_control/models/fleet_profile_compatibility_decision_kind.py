from typing import Literal, cast

FleetProfileCompatibilityDecisionKind = Literal['engine-generation', 'jit', 'tuning']

FLEET_PROFILE_COMPATIBILITY_DECISION_KIND_VALUES: set[FleetProfileCompatibilityDecisionKind] = { 'engine-generation', 'jit', 'tuning',  }

def check_fleet_profile_compatibility_decision_kind(value: str) -> FleetProfileCompatibilityDecisionKind:
    if value in FLEET_PROFILE_COMPATIBILITY_DECISION_KIND_VALUES:
        return cast(FleetProfileCompatibilityDecisionKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_COMPATIBILITY_DECISION_KIND_VALUES!r}")
