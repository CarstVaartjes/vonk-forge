from typing import Literal, cast

AgentUpgradePreviewResponseStrategy = Literal['all-at-once', 'one-at-a-time']

AGENT_UPGRADE_PREVIEW_RESPONSE_STRATEGY_VALUES: set[AgentUpgradePreviewResponseStrategy] = { 'all-at-once', 'one-at-a-time',  }

def check_agent_upgrade_preview_response_strategy(value: str) -> AgentUpgradePreviewResponseStrategy:
    if value in AGENT_UPGRADE_PREVIEW_RESPONSE_STRATEGY_VALUES:
        return cast(AgentUpgradePreviewResponseStrategy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_UPGRADE_PREVIEW_RESPONSE_STRATEGY_VALUES!r}")
