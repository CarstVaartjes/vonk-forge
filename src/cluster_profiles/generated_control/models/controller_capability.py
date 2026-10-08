from typing import Literal

ControllerCapability = Literal['agent-presence', 'agent-proxy-auth', 'artifact-storage', 'browser-auth', 'certificate-authority', 'cursor-auth', 'distribution', 'enrollment-bootstrap', 'fabric-policy', 'gateway-keys', 'host-runtime-authority', 'image-collection', 'management-policy', 'metrics-auth', 'model-cache', 'recipe-library', 'recipe-routes', 'route-publisher', 'runtime-image-storage', 'token-auth']

CONTROLLER_CAPABILITY_VALUES: set[ControllerCapability] = { 'agent-presence', 'agent-proxy-auth', 'artifact-storage', 'browser-auth', 'certificate-authority', 'cursor-auth', 'distribution', 'enrollment-bootstrap', 'fabric-policy', 'gateway-keys', 'host-runtime-authority', 'image-collection', 'management-policy', 'metrics-auth', 'model-cache', 'recipe-library', 'recipe-routes', 'route-publisher', 'runtime-image-storage', 'token-auth',  }

def check_controller_capability(value: str) -> ControllerCapability:
    if value in CONTROLLER_CAPABILITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTROLLER_CAPABILITY_VALUES!r}")
