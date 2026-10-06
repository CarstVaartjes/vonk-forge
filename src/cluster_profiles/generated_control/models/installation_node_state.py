from typing import Literal

InstallationNodeState = Literal['failed', 'installed', 'planned', 'uninstalled']

INSTALLATION_NODE_STATE_VALUES: set[InstallationNodeState] = { 'failed', 'installed', 'planned', 'uninstalled',  }

def check_installation_node_state(value: str) -> InstallationNodeState:
    if value in INSTALLATION_NODE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALLATION_NODE_STATE_VALUES!r}")
