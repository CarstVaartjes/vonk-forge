from typing import Literal

FleetProfileEffectProgressState = Literal['cancelled', 'failed', 'not-issued', 'pending', 'succeeded', 'unknown']

FLEET_PROFILE_EFFECT_PROGRESS_STATE_VALUES: set[FleetProfileEffectProgressState] = { 'cancelled', 'failed', 'not-issued', 'pending', 'succeeded', 'unknown',  }

def check_fleet_profile_effect_progress_state(value: str) -> FleetProfileEffectProgressState:
    if value in FLEET_PROFILE_EFFECT_PROGRESS_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_EFFECT_PROGRESS_STATE_VALUES!r}")
