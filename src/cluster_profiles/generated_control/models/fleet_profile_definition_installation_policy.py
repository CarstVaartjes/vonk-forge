from typing import Literal, cast

FleetProfileDefinitionInstallationPolicy = Literal['exact', 'keep-cached']

FLEET_PROFILE_DEFINITION_INSTALLATION_POLICY_VALUES: set[FleetProfileDefinitionInstallationPolicy] = { 'exact', 'keep-cached',  }

def check_fleet_profile_definition_installation_policy(value: str) -> FleetProfileDefinitionInstallationPolicy:
    if value in FLEET_PROFILE_DEFINITION_INSTALLATION_POLICY_VALUES:
        return cast(FleetProfileDefinitionInstallationPolicy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_DEFINITION_INSTALLATION_POLICY_VALUES!r}")
