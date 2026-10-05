from typing import Literal

FleetProfileApplicationProgressSupersedeCodeType0 = Literal['effects-changed-during-admission', 'superseded-by-intent', 'superseded-by-retry']

FLEET_PROFILE_APPLICATION_PROGRESS_SUPERSEDE_CODE_TYPE_0_VALUES: set[FleetProfileApplicationProgressSupersedeCodeType0] = { 'effects-changed-during-admission', 'superseded-by-intent', 'superseded-by-retry',  }

def check_fleet_profile_application_progress_supersede_code_type_0(value: str) -> FleetProfileApplicationProgressSupersedeCodeType0:
    if value in FLEET_PROFILE_APPLICATION_PROGRESS_SUPERSEDE_CODE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_PROGRESS_SUPERSEDE_CODE_TYPE_0_VALUES!r}")
