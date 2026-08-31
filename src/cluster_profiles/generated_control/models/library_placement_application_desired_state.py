from typing import Literal, cast

LibraryPlacementApplicationDesiredState = Literal['installed', 'running']

LIBRARY_PLACEMENT_APPLICATION_DESIRED_STATE_VALUES: set[LibraryPlacementApplicationDesiredState] = { 'installed', 'running',  }

def check_library_placement_application_desired_state(value: str) -> LibraryPlacementApplicationDesiredState:
    if value in LIBRARY_PLACEMENT_APPLICATION_DESIRED_STATE_VALUES:
        return cast(LibraryPlacementApplicationDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_APPLICATION_DESIRED_STATE_VALUES!r}")
