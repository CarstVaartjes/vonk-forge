from typing import Literal

FleetProfileApplicationViewReasonCodeType0 = Literal['effects-changed-during-admission', 'superseded-by-intent', 'superseded-by-retry']

FLEET_PROFILE_APPLICATION_VIEW_REASON_CODE_TYPE_0_VALUES: set[FleetProfileApplicationViewReasonCodeType0] = { 'effects-changed-during-admission', 'superseded-by-intent', 'superseded-by-retry',  }

def check_fleet_profile_application_view_reason_code_type_0(value: str) -> FleetProfileApplicationViewReasonCodeType0:
    if value in FLEET_PROFILE_APPLICATION_VIEW_REASON_CODE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_VIEW_REASON_CODE_TYPE_0_VALUES!r}")
