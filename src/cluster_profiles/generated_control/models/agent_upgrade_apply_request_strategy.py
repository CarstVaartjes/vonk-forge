from typing import Literal, cast

AgentUpgradeApplyRequestStrategy = Literal['all-at-once', 'one-at-a-time']

AGENT_UPGRADE_APPLY_REQUEST_STRATEGY_VALUES: set[AgentUpgradeApplyRequestStrategy] = { 'all-at-once', 'one-at-a-time',  }

def check_agent_upgrade_apply_request_strategy(value: str) -> AgentUpgradeApplyRequestStrategy:
    if value in AGENT_UPGRADE_APPLY_REQUEST_STRATEGY_VALUES:
        return cast(AgentUpgradeApplyRequestStrategy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_UPGRADE_APPLY_REQUEST_STRATEGY_VALUES!r}")
