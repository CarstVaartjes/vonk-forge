from typing import Literal, cast

LibraryPlacementApplicationState = Literal['cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator']

LIBRARY_PLACEMENT_APPLICATION_STATE_VALUES: set[LibraryPlacementApplicationState] = { 'cancelled', 'failed', 'queued', 'running', 'succeeded', 'waiting-for-operator',  }

def check_library_placement_application_state(value: str) -> LibraryPlacementApplicationState:
    if value in LIBRARY_PLACEMENT_APPLICATION_STATE_VALUES:
        return cast(LibraryPlacementApplicationState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_APPLICATION_STATE_VALUES!r}")
