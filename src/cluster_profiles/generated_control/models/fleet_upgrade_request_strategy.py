from typing import Literal, cast

FleetUpgradeRequestStrategy = Literal['all-at-once', 'one-at-a-time']

FLEET_UPGRADE_REQUEST_STRATEGY_VALUES: set[FleetUpgradeRequestStrategy] = { 'all-at-once', 'one-at-a-time',  }

def check_fleet_upgrade_request_strategy(value: str) -> FleetUpgradeRequestStrategy:
    if value in FLEET_UPGRADE_REQUEST_STRATEGY_VALUES:
        return cast(FleetUpgradeRequestStrategy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_UPGRADE_REQUEST_STRATEGY_VALUES!r}")
