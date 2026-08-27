from typing import Literal, cast

AgentUpgradePreviewRequestStrategy = Literal['all-at-once', 'one-at-a-time']

AGENT_UPGRADE_PREVIEW_REQUEST_STRATEGY_VALUES: set[AgentUpgradePreviewRequestStrategy] = { 'all-at-once', 'one-at-a-time',  }

def check_agent_upgrade_preview_request_strategy(value: str) -> AgentUpgradePreviewRequestStrategy:
    if value in AGENT_UPGRADE_PREVIEW_REQUEST_STRATEGY_VALUES:
        return cast(AgentUpgradePreviewRequestStrategy, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_UPGRADE_PREVIEW_REQUEST_STRATEGY_VALUES!r}")
