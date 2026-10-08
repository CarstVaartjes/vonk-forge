from typing import Literal

ProfileEffectState = Literal['cancelled', 'failed', 'not-issued', 'pending', 'succeeded', 'unknown']

PROFILE_EFFECT_STATE_VALUES: set[ProfileEffectState] = { 'cancelled', 'failed', 'not-issued', 'pending', 'succeeded', 'unknown',  }

def check_profile_effect_state(value: str) -> ProfileEffectState:
    if value in PROFILE_EFFECT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_EFFECT_STATE_VALUES!r}")
