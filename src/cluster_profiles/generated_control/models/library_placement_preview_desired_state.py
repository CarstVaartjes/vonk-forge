from typing import Literal, cast

LibraryPlacementPreviewDesiredState = Literal['installed', 'running']

LIBRARY_PLACEMENT_PREVIEW_DESIRED_STATE_VALUES: set[LibraryPlacementPreviewDesiredState] = { 'installed', 'running',  }

def check_library_placement_preview_desired_state(value: str) -> LibraryPlacementPreviewDesiredState:
    if value in LIBRARY_PLACEMENT_PREVIEW_DESIRED_STATE_VALUES:
        return cast(LibraryPlacementPreviewDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_PREVIEW_DESIRED_STATE_VALUES!r}")
