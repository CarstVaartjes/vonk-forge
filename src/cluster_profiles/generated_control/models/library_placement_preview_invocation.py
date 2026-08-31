from typing import Literal, cast

LibraryPlacementPreviewInvocation = Literal['button', 'drag-drop', 'keyboard']

LIBRARY_PLACEMENT_PREVIEW_INVOCATION_VALUES: set[LibraryPlacementPreviewInvocation] = { 'button', 'drag-drop', 'keyboard',  }

def check_library_placement_preview_invocation(value: str) -> LibraryPlacementPreviewInvocation:
    if value in LIBRARY_PLACEMENT_PREVIEW_INVOCATION_VALUES:
        return cast(LibraryPlacementPreviewInvocation, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_PREVIEW_INVOCATION_VALUES!r}")
