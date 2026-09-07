from typing import Literal, cast

FleetProfileIntendedConfigurationInstallationPolicy = Literal['exact', 'keep-cached']

FLEET_PROFILE_INTENDED_CONFIGURATION_INSTALLATION_POLICY_VALUES: set[FleetProfileIntendedConfigurationInstallationPolicy] = { 'exact', 'keep-cached',  }

def check_fleet_profile_intended_configuration_installation_policy(value: str) -> FleetProfileIntendedConfigurationInstallationPolicy:
    if value in FLEET_PROFILE_INTENDED_CONFIGURATION_INSTALLATION_POLICY_VALUES:
        return cast(FleetProfileIntendedConfigurationInstallationPolicy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_INTENDED_CONFIGURATION_INSTALLATION_POLICY_VALUES!r}")
