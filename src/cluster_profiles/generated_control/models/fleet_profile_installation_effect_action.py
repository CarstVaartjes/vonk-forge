from typing import Literal, cast

FleetProfileInstallationEffectAction = Literal['keep', 'remove']

FLEET_PROFILE_INSTALLATION_EFFECT_ACTION_VALUES: set[FleetProfileInstallationEffectAction] = { 'keep', 'remove',  }

def check_fleet_profile_installation_effect_action(value: str) -> FleetProfileInstallationEffectAction:
    if value in FLEET_PROFILE_INSTALLATION_EFFECT_ACTION_VALUES:
        return cast(FleetProfileInstallationEffectAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_INSTALLATION_EFFECT_ACTION_VALUES!r}")
