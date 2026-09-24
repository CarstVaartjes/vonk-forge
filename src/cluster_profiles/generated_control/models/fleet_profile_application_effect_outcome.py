from typing import Literal, cast

FleetProfileApplicationEffectOutcome = Literal['cancelled', 'failed', 'not-issued', 'pending', 'succeeded']

FLEET_PROFILE_APPLICATION_EFFECT_OUTCOME_VALUES: set[FleetProfileApplicationEffectOutcome] = { 'cancelled', 'failed', 'not-issued', 'pending', 'succeeded',  }

def check_fleet_profile_application_effect_outcome(value: str) -> FleetProfileApplicationEffectOutcome:
    if value in FLEET_PROFILE_APPLICATION_EFFECT_OUTCOME_VALUES:
        return cast(FleetProfileApplicationEffectOutcome, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_EFFECT_OUTCOME_VALUES!r}")
