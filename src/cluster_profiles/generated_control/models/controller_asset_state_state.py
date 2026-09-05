from typing import Literal, cast

ControllerAssetStateState = Literal['failed', 'missing', 'preparing', 'ready', 'unknown', 'unsupported', 'verifying']

CONTROLLER_ASSET_STATE_STATE_VALUES: set[ControllerAssetStateState] = { 'failed', 'missing', 'preparing', 'ready', 'unknown', 'unsupported', 'verifying',  }

def check_controller_asset_state_state(value: str) -> ControllerAssetStateState:
    if value in CONTROLLER_ASSET_STATE_STATE_VALUES:
        return cast(ControllerAssetStateState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTROLLER_ASSET_STATE_STATE_VALUES!r}")
