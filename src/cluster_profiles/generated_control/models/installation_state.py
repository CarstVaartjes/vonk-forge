from typing import Literal

InstallationState = Literal['failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled']

INSTALLATION_STATE_VALUES: set[InstallationState] = { 'failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled',  }

def check_installation_state(value: str) -> InstallationState:
    if value in INSTALLATION_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALLATION_STATE_VALUES!r}")
