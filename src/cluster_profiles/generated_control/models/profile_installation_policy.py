from typing import Literal

ProfileInstallationPolicy = Literal['exact', 'keep-cached']

PROFILE_INSTALLATION_POLICY_VALUES: set[ProfileInstallationPolicy] = { 'exact', 'keep-cached',  }

def check_profile_installation_policy(value: str) -> ProfileInstallationPolicy:
    if value in PROFILE_INSTALLATION_POLICY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_INSTALLATION_POLICY_VALUES!r}")
