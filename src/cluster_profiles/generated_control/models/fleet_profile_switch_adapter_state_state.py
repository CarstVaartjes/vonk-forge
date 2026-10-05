from typing import Literal

FleetProfileSwitchAdapterStateState = Literal['cancelled', 'failed', 'queued', 'running', 'succeeded', 'superseded', 'waiting-for-operator']

FLEET_PROFILE_SWITCH_ADAPTER_STATE_STATE_VALUES: set[FleetProfileSwitchAdapterStateState] = { 'cancelled', 'failed', 'queued', 'running', 'succeeded', 'superseded', 'waiting-for-operator',  }

def check_fleet_profile_switch_adapter_state_state(value: str) -> FleetProfileSwitchAdapterStateState:
    if value in FLEET_PROFILE_SWITCH_ADAPTER_STATE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_SWITCH_ADAPTER_STATE_STATE_VALUES!r}")
