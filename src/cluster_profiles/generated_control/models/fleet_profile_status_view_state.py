from typing import Literal, cast

FleetProfileStatusViewState = Literal['blocked', 'draft', 'drifted', 'matched', 'needs-preparation', 'partially-applied', 'ready', 'switching']

FLEET_PROFILE_STATUS_VIEW_STATE_VALUES: set[FleetProfileStatusViewState] = { 'blocked', 'draft', 'drifted', 'matched', 'needs-preparation', 'partially-applied', 'ready', 'switching',  }

def check_fleet_profile_status_view_state(value: str) -> FleetProfileStatusViewState:
    if value in FLEET_PROFILE_STATUS_VIEW_STATE_VALUES:
        return cast(FleetProfileStatusViewState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_STATUS_VIEW_STATE_VALUES!r}")
