from typing import Literal, cast

NodeConnectionAgentState = Literal['active', 'pending', 'retired', 'revoked', 'unregistered']

NODE_CONNECTION_AGENT_STATE_VALUES: set[NodeConnectionAgentState] = { 'active', 'pending', 'retired', 'revoked', 'unregistered',  }

def check_node_connection_agent_state(value: str) -> NodeConnectionAgentState:
    if value in NODE_CONNECTION_AGENT_STATE_VALUES:
        return cast(NodeConnectionAgentState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NODE_CONNECTION_AGENT_STATE_VALUES!r}")
