from typing import Literal, cast

BuildNetworkMode = Literal['none', 'public']

BUILD_NETWORK_MODE_VALUES: set[BuildNetworkMode] = { 'none', 'public',  }

def check_build_network_mode(value: str) -> BuildNetworkMode:
    if value in BUILD_NETWORK_MODE_VALUES:
        return cast(BuildNetworkMode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {BUILD_NETWORK_MODE_VALUES!r}")
