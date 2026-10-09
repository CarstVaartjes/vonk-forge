from typing import Literal

NodeIdentityState = Literal['active', 'retired']

NODE_IDENTITY_STATE_VALUES: set[NodeIdentityState] = { 'active', 'retired',  }

def check_node_identity_state(value: str) -> NodeIdentityState:
    if value in NODE_IDENTITY_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NODE_IDENTITY_STATE_VALUES!r}")
