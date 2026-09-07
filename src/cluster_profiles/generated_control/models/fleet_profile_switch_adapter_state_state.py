from typing import Literal, cast

FleetProfileSwitchAdapterStateState = Literal['cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator']

FLEET_PROFILE_SWITCH_ADAPTER_STATE_STATE_VALUES: set[FleetProfileSwitchAdapterStateState] = { 'cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator',  }

def check_fleet_profile_switch_adapter_state_state(value: str) -> FleetProfileSwitchAdapterStateState:
    if value in FLEET_PROFILE_SWITCH_ADAPTER_STATE_STATE_VALUES:
        return cast(FleetProfileSwitchAdapterStateState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_ADAPTER_STATE_STATE_VALUES!r}")
