from typing import Literal

PlacementInstallState = Literal['complete', 'not_present', 'partial', 'unknown']

PLACEMENT_INSTALL_STATE_VALUES: set[PlacementInstallState] = { 'complete', 'not_present', 'partial', 'unknown',  }

def check_placement_install_state(value: str) -> PlacementInstallState:
    if value in PLACEMENT_INSTALL_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_INSTALL_STATE_VALUES!r}")
