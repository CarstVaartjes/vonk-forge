from typing import Literal, cast

FleetProfileSwitchChildStateKind = Literal['run', 'stop']

FLEET_PROFILE_SWITCH_CHILD_STATE_KIND_VALUES: set[FleetProfileSwitchChildStateKind] = { 'run', 'stop',  }

def check_fleet_profile_switch_child_state_kind(value: str) -> FleetProfileSwitchChildStateKind:
    if value in FLEET_PROFILE_SWITCH_CHILD_STATE_KIND_VALUES:
        return cast(FleetProfileSwitchChildStateKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_CHILD_STATE_KIND_VALUES!r}")
