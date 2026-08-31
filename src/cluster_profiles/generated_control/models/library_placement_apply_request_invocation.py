from typing import Literal, cast

LibraryPlacementApplyRequestInvocation = Literal['button', 'drag-drop', 'keyboard']

LIBRARY_PLACEMENT_APPLY_REQUEST_INVOCATION_VALUES: set[LibraryPlacementApplyRequestInvocation] = { 'button', 'drag-drop', 'keyboard',  }

def check_library_placement_apply_request_invocation(value: str) -> LibraryPlacementApplyRequestInvocation:
    if value in LIBRARY_PLACEMENT_APPLY_REQUEST_INVOCATION_VALUES:
        return cast(LibraryPlacementApplyRequestInvocation, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_APPLY_REQUEST_INVOCATION_VALUES!r}")
