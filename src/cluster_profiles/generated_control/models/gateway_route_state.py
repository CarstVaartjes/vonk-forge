from typing import Literal

GatewayRouteState = Literal['maintenance', 'published', 'unavailable']

GATEWAY_ROUTE_STATE_VALUES: set[GatewayRouteState] = { 'maintenance', 'published', 'unavailable',  }

def check_gateway_route_state(value: str) -> GatewayRouteState:
    if value in GATEWAY_ROUTE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GATEWAY_ROUTE_STATE_VALUES!r}")
