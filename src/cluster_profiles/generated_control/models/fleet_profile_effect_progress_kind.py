from typing import Literal

FleetProfileEffectProgressKind = Literal['cleanup', 'install', 'run', 'stop']

FLEET_PROFILE_EFFECT_PROGRESS_KIND_VALUES: set[FleetProfileEffectProgressKind] = { 'cleanup', 'install', 'run', 'stop',  }

def check_fleet_profile_effect_progress_kind(value: str) -> FleetProfileEffectProgressKind:
    if value in FLEET_PROFILE_EFFECT_PROGRESS_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_EFFECT_PROGRESS_KIND_VALUES!r}")
