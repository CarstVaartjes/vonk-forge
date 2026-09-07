from typing import Literal, cast

FleetProfileSwitchAdapterStateActiveKindType0 = Literal['run', 'stop']

FLEET_PROFILE_SWITCH_ADAPTER_STATE_ACTIVE_KIND_TYPE_0_VALUES: set[FleetProfileSwitchAdapterStateActiveKindType0] = { 'run', 'stop',  }

def check_fleet_profile_switch_adapter_state_active_kind_type_0(value: str) -> FleetProfileSwitchAdapterStateActiveKindType0:
    if value in FLEET_PROFILE_SWITCH_ADAPTER_STATE_ACTIVE_KIND_TYPE_0_VALUES:
        return cast(FleetProfileSwitchAdapterStateActiveKindType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_ADAPTER_STATE_ACTIVE_KIND_TYPE_0_VALUES!r}")
