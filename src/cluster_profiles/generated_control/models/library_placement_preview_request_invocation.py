from typing import Literal, cast

LibraryPlacementPreviewRequestInvocation = Literal['button', 'drag-drop', 'keyboard']

LIBRARY_PLACEMENT_PREVIEW_REQUEST_INVOCATION_VALUES: set[LibraryPlacementPreviewRequestInvocation] = { 'button', 'drag-drop', 'keyboard',  }

def check_library_placement_preview_request_invocation(value: str) -> LibraryPlacementPreviewRequestInvocation:
    if value in LIBRARY_PLACEMENT_PREVIEW_REQUEST_INVOCATION_VALUES:
        return cast(LibraryPlacementPreviewRequestInvocation, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_PREVIEW_REQUEST_INVOCATION_VALUES!r}")
