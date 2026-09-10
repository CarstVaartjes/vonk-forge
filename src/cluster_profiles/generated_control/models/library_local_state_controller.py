from typing import Literal, cast

LibraryLocalStateController = Literal['cached', 'failed', 'not_cached', 'preparing', 'unknown']

LIBRARY_LOCAL_STATE_CONTROLLER_VALUES: set[LibraryLocalStateController] = { 'cached', 'failed', 'not_cached', 'preparing', 'unknown',  }

def check_library_local_state_controller(value: str) -> LibraryLocalStateController:
    if value in LIBRARY_LOCAL_STATE_CONTROLLER_VALUES:
        return cast(LibraryLocalStateController, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_LOCAL_STATE_CONTROLLER_VALUES!r}")
