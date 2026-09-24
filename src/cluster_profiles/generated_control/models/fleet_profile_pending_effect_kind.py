from typing import Literal, cast

FleetProfilePendingEffectKind = Literal['job', 'profile-application']

FLEET_PROFILE_PENDING_EFFECT_KIND_VALUES: set[FleetProfilePendingEffectKind] = { 'job', 'profile-application',  }

def check_fleet_profile_pending_effect_kind(value: str) -> FleetProfilePendingEffectKind:
    if value in FLEET_PROFILE_PENDING_EFFECT_KIND_VALUES:
        return cast(FleetProfilePendingEffectKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_PENDING_EFFECT_KIND_VALUES!r}")
