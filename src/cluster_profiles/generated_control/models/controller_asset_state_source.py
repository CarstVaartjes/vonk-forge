from typing import Literal

ControllerAssetStateSource = Literal['controller-build', 'nas-cache', 'unknown']

CONTROLLER_ASSET_STATE_SOURCE_VALUES: set[ControllerAssetStateSource] = { 'controller-build', 'nas-cache', 'unknown',  }

def check_controller_asset_state_source(value: str) -> ControllerAssetStateSource:
    if value in CONTROLLER_ASSET_STATE_SOURCE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTROLLER_ASSET_STATE_SOURCE_VALUES!r}")
