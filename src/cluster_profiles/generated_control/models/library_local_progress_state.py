from typing import Literal

LibraryLocalProgressState = Literal['backoff', 'cancelled', 'failed', 'queued', 'running', 'succeeded']

LIBRARY_LOCAL_PROGRESS_STATE_VALUES: set[LibraryLocalProgressState] = { 'backoff', 'cancelled', 'failed', 'queued', 'running', 'succeeded',  }

def check_library_local_progress_state(value: str) -> LibraryLocalProgressState:
    if value in LIBRARY_LOCAL_PROGRESS_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_LOCAL_PROGRESS_STATE_VALUES!r}")
