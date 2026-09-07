from typing import Literal, cast

FleetProfileSwitchChildStateState = Literal['cancelled', 'failed', 'succeeded']

FLEET_PROFILE_SWITCH_CHILD_STATE_STATE_VALUES: set[FleetProfileSwitchChildStateState] = { 'cancelled', 'failed', 'succeeded',  }

def check_fleet_profile_switch_child_state_state(value: str) -> FleetProfileSwitchChildStateState:
    if value in FLEET_PROFILE_SWITCH_CHILD_STATE_STATE_VALUES:
        return cast(FleetProfileSwitchChildStateState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_CHILD_STATE_STATE_VALUES!r}")
