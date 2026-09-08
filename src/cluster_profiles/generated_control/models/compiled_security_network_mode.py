from typing import Literal, cast

CompiledSecurityNetworkMode = Literal['bridge', 'none']

COMPILED_SECURITY_NETWORK_MODE_VALUES: set[CompiledSecurityNetworkMode] = { 'bridge', 'none',  }

def check_compiled_security_network_mode(value: str) -> CompiledSecurityNetworkMode:
    if value in COMPILED_SECURITY_NETWORK_MODE_VALUES:
        return cast(CompiledSecurityNetworkMode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_SECURITY_NETWORK_MODE_VALUES!r}")
