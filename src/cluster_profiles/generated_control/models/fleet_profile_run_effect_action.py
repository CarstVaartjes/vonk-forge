from typing import Literal, cast

FleetProfileRunEffectAction = Literal['keep', 'stop']

FLEET_PROFILE_RUN_EFFECT_ACTION_VALUES: set[FleetProfileRunEffectAction] = { 'keep', 'stop',  }

def check_fleet_profile_run_effect_action(value: str) -> FleetProfileRunEffectAction:
    if value in FLEET_PROFILE_RUN_EFFECT_ACTION_VALUES:
        return cast(FleetProfileRunEffectAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_RUN_EFFECT_ACTION_VALUES!r}")
