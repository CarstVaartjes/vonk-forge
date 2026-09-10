from typing import Literal, cast

LibraryLocalProgressState = Literal['failed', 'partial', 'queued', 'running', 'succeeded']

LIBRARY_LOCAL_PROGRESS_STATE_VALUES: set[LibraryLocalProgressState] = { 'failed', 'partial', 'queued', 'running', 'succeeded',  }

def check_library_local_progress_state(value: str) -> LibraryLocalProgressState:
    if value in LIBRARY_LOCAL_PROGRESS_STATE_VALUES:
        return cast(LibraryLocalProgressState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_LOCAL_PROGRESS_STATE_VALUES!r}")
