from typing import Literal

CompiledSecurityNetworkMode = Literal['bridge', 'host', 'none']

COMPILED_SECURITY_NETWORK_MODE_VALUES: set[CompiledSecurityNetworkMode] = { 'bridge', 'host', 'none',  }

def check_compiled_security_network_mode(value: str) -> CompiledSecurityNetworkMode:
    if value in COMPILED_SECURITY_NETWORK_MODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_SECURITY_NETWORK_MODE_VALUES!r}")
