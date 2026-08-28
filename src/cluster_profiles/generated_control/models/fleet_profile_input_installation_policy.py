from typing import Literal, cast

FleetProfileInputInstallationPolicy = Literal['exact', 'keep-cached']

FLEET_PROFILE_INPUT_INSTALLATION_POLICY_VALUES: set[FleetProfileInputInstallationPolicy] = { 'exact', 'keep-cached',  }

def check_fleet_profile_input_installation_policy(value: str) -> FleetProfileInputInstallationPolicy:
    if value in FLEET_PROFILE_INPUT_INSTALLATION_POLICY_VALUES:
        return cast(FleetProfileInputInstallationPolicy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_INPUT_INSTALLATION_POLICY_VALUES!r}")
