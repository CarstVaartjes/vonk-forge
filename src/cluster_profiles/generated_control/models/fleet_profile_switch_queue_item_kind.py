from typing import Literal, cast

FleetProfileSwitchQueueItemKind = Literal['cleanup', 'install', 'run', 'stop']

FLEET_PROFILE_SWITCH_QUEUE_ITEM_KIND_VALUES: set[FleetProfileSwitchQueueItemKind] = { 'cleanup', 'install', 'run', 'stop',  }

def check_fleet_profile_switch_queue_item_kind(value: str) -> FleetProfileSwitchQueueItemKind:
    if value in FLEET_PROFILE_SWITCH_QUEUE_ITEM_KIND_VALUES:
        return cast(FleetProfileSwitchQueueItemKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_QUEUE_ITEM_KIND_VALUES!r}")
