from typing import Literal, cast

OperationalInstallationState = Literal['failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled']

OPERATIONAL_INSTALLATION_STATE_VALUES: set[OperationalInstallationState] = { 'failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled',  }

def check_operational_installation_state(value: str) -> OperationalInstallationState:
    if value in OPERATIONAL_INSTALLATION_STATE_VALUES:
        return cast(OperationalInstallationState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATIONAL_INSTALLATION_STATE_VALUES!r}")
