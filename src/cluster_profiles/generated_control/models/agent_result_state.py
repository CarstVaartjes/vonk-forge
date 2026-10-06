from typing import Literal

AgentResultState = Literal['cancelled', 'failed', 'observing', 'succeeded']

AGENT_RESULT_STATE_VALUES: set[AgentResultState] = { 'cancelled', 'failed', 'observing', 'succeeded',  }

def check_agent_result_state(value: str) -> AgentResultState:
    if value in AGENT_RESULT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_RESULT_STATE_VALUES!r}")
