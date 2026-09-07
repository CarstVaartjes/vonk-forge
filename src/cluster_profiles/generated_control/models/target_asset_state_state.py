from typing import Literal, cast

TargetAssetStateState = Literal['failed', 'missing', 'preparing', 'ready', 'unknown', 'unsupported', 'verifying']

TARGET_ASSET_STATE_STATE_VALUES: set[TargetAssetStateState] = { 'failed', 'missing', 'preparing', 'ready', 'unknown', 'unsupported', 'verifying',  }

def check_target_asset_state_state(value: str) -> TargetAssetStateState:
    if value in TARGET_ASSET_STATE_STATE_VALUES:
        return cast(TargetAssetStateState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TARGET_ASSET_STATE_STATE_VALUES!r}")
