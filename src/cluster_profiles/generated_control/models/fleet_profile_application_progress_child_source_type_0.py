from typing import Literal, cast

FleetProfileApplicationProgressChildSourceType0 = Literal['recipe', 'switch-adapter']

FLEET_PROFILE_APPLICATION_PROGRESS_CHILD_SOURCE_TYPE_0_VALUES: set[FleetProfileApplicationProgressChildSourceType0] = { 'recipe', 'switch-adapter',  }

def check_fleet_profile_application_progress_child_source_type_0(value: str) -> FleetProfileApplicationProgressChildSourceType0:
    if value in FLEET_PROFILE_APPLICATION_PROGRESS_CHILD_SOURCE_TYPE_0_VALUES:
        return cast(FleetProfileApplicationProgressChildSourceType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_APPLICATION_PROGRESS_CHILD_SOURCE_TYPE_0_VALUES!r}")
