from typing import Literal, cast

FleetProfileCaptureInputInstallationPolicy = Literal['exact', 'keep-cached']

FLEET_PROFILE_CAPTURE_INPUT_INSTALLATION_POLICY_VALUES: set[FleetProfileCaptureInputInstallationPolicy] = { 'exact', 'keep-cached',  }

def check_fleet_profile_capture_input_installation_policy(value: str) -> FleetProfileCaptureInputInstallationPolicy:
    if value in FLEET_PROFILE_CAPTURE_INPUT_INSTALLATION_POLICY_VALUES:
        return cast(FleetProfileCaptureInputInstallationPolicy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_CAPTURE_INPUT_INSTALLATION_POLICY_VALUES!r}")
