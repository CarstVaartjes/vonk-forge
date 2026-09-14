from typing import Literal, cast

AgentFailureKind = Literal['integrity-failure', 'invalid-authority', 'invalid-contract', 'resource-prerequisite', 'temporary-dependency', 'uncertain-effect']

AGENT_FAILURE_KIND_VALUES: set[AgentFailureKind] = { 'integrity-failure', 'invalid-authority', 'invalid-contract', 'resource-prerequisite', 'temporary-dependency', 'uncertain-effect',  }

def check_agent_failure_kind(value: str) -> AgentFailureKind:
    if value in AGENT_FAILURE_KIND_VALUES:
        return cast(AgentFailureKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_FAILURE_KIND_VALUES!r}")
