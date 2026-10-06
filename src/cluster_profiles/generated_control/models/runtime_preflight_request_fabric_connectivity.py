from typing import Literal

RuntimePreflightRequestFabricConnectivity = Literal['connected', 'none']

RUNTIME_PREFLIGHT_REQUEST_FABRIC_CONNECTIVITY_VALUES: set[RuntimePreflightRequestFabricConnectivity] = { 'connected', 'none',  }

def check_runtime_preflight_request_fabric_connectivity(value: str) -> RuntimePreflightRequestFabricConnectivity:
    if value in RUNTIME_PREFLIGHT_REQUEST_FABRIC_CONNECTIVITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_PREFLIGHT_REQUEST_FABRIC_CONNECTIVITY_VALUES!r}")
