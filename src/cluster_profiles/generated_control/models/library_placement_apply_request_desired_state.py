from typing import Literal, cast

LibraryPlacementApplyRequestDesiredState = Literal['installed', 'running']

LIBRARY_PLACEMENT_APPLY_REQUEST_DESIRED_STATE_VALUES: set[LibraryPlacementApplyRequestDesiredState] = { 'installed', 'running',  }

def check_library_placement_apply_request_desired_state(value: str) -> LibraryPlacementApplyRequestDesiredState:
    if value in LIBRARY_PLACEMENT_APPLY_REQUEST_DESIRED_STATE_VALUES:
        return cast(LibraryPlacementApplyRequestDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_APPLY_REQUEST_DESIRED_STATE_VALUES!r}")
