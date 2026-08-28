from typing import Literal, cast

FleetProfileViewInstallationPolicy = Literal['exact', 'keep-cached']

FLEET_PROFILE_VIEW_INSTALLATION_POLICY_VALUES: set[FleetProfileViewInstallationPolicy] = { 'exact', 'keep-cached',  }

def check_fleet_profile_view_installation_policy(value: str) -> FleetProfileViewInstallationPolicy:
    if value in FLEET_PROFILE_VIEW_INSTALLATION_POLICY_VALUES:
        return cast(FleetProfileViewInstallationPolicy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_VIEW_INSTALLATION_POLICY_VALUES!r}")
