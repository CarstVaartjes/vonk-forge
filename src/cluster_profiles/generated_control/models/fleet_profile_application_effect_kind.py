from typing import Literal, cast

FleetProfileApplicationEffectKind = Literal['agent-operation', 'cleanup', 'install', 'profile-step', 'run', 'stop']

FLEET_PROFILE_APPLICATION_EFFECT_KIND_VALUES: set[FleetProfileApplicationEffectKind] = { 'agent-operation', 'cleanup', 'install', 'profile-step', 'run', 'stop',  }

def check_fleet_profile_application_effect_kind(value: str) -> FleetProfileApplicationEffectKind:
    if value in FLEET_PROFILE_APPLICATION_EFFECT_KIND_VALUES:
        return cast(FleetProfileApplicationEffectKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_EFFECT_KIND_VALUES!r}")
