from typing import Literal

AgentTransportKind = Literal['body', 'connect', 'protocol', 'timeout', 'unknown']

AGENT_TRANSPORT_KIND_VALUES: set[AgentTransportKind] = { 'body', 'connect', 'protocol', 'timeout', 'unknown',  }

def check_agent_transport_kind(value: str) -> AgentTransportKind:
    if value in AGENT_TRANSPORT_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_TRANSPORT_KIND_VALUES!r}")
