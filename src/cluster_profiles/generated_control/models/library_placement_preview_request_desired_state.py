from typing import Literal, cast

LibraryPlacementPreviewRequestDesiredState = Literal['installed', 'running']

LIBRARY_PLACEMENT_PREVIEW_REQUEST_DESIRED_STATE_VALUES: set[LibraryPlacementPreviewRequestDesiredState] = { 'installed', 'running',  }

def check_library_placement_preview_request_desired_state(value: str) -> LibraryPlacementPreviewRequestDesiredState:
    if value in LIBRARY_PLACEMENT_PREVIEW_REQUEST_DESIRED_STATE_VALUES:
        return cast(LibraryPlacementPreviewRequestDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_PREVIEW_REQUEST_DESIRED_STATE_VALUES!r}")
