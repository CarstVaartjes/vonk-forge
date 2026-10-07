from typing import Literal

FleetProfileSwitchPendingChildKind = Literal['cleanup', 'install', 'run', 'stop']

FLEET_PROFILE_SWITCH_PENDING_CHILD_KIND_VALUES: set[FleetProfileSwitchPendingChildKind] = { 'cleanup', 'install', 'run', 'stop',  }

def check_fleet_profile_switch_pending_child_kind(value: str) -> FleetProfileSwitchPendingChildKind:
    if value in FLEET_PROFILE_SWITCH_PENDING_CHILD_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_PENDING_CHILD_KIND_VALUES!r}")
