from typing import Literal, cast

FleetActionResponseAction = Literal['enroll', 're-enroll', 'remove', 'upgrade']

FLEET_ACTION_RESPONSE_ACTION_VALUES: set[FleetActionResponseAction] = { 'enroll', 're-enroll', 'remove', 'upgrade',  }

def check_fleet_action_response_action(value: str) -> FleetActionResponseAction:
    if value in FLEET_ACTION_RESPONSE_ACTION_VALUES:
        return cast(FleetActionResponseAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_ACTION_RESPONSE_ACTION_VALUES!r}")
