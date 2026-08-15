from typing import Literal, cast

NodeConnectionOnlineState = Literal['offline', 'online', 'unregistered']

NODE_CONNECTION_ONLINE_STATE_VALUES: set[NodeConnectionOnlineState] = { 'offline', 'online', 'unregistered',  }

def check_node_connection_online_state(value: str) -> NodeConnectionOnlineState:
    if value in NODE_CONNECTION_ONLINE_STATE_VALUES:
        return cast(NodeConnectionOnlineState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NODE_CONNECTION_ONLINE_STATE_VALUES!r}")
