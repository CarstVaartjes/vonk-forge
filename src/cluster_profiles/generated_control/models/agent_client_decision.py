from typing import Literal

AgentClientDecision = Literal['defer', 'exit', 'record', 'retry']

AGENT_CLIENT_DECISION_VALUES: set[AgentClientDecision] = { 'defer', 'exit', 'record', 'retry',  }

def check_agent_client_decision(value: str) -> AgentClientDecision:
    if value in AGENT_CLIENT_DECISION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_CLIENT_DECISION_VALUES!r}")
