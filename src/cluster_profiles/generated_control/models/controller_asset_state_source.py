from typing import Literal, cast

ControllerAssetStateSource = Literal['controller-build', 'nas-cache', 'published', 'unknown']

CONTROLLER_ASSET_STATE_SOURCE_VALUES: set[ControllerAssetStateSource] = { 'controller-build', 'nas-cache', 'published', 'unknown',  }

def check_controller_asset_state_source(value: str) -> ControllerAssetStateSource:
    if value in CONTROLLER_ASSET_STATE_SOURCE_VALUES:
        return cast(ControllerAssetStateSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTROLLER_ASSET_STATE_SOURCE_VALUES!r}")
