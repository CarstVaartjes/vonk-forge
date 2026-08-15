from typing import Literal, cast

LibraryRunSummaryState = Literal['planned', 'running', 'starting', 'stopping']

LIBRARY_RUN_SUMMARY_STATE_VALUES: set[LibraryRunSummaryState] = { 'planned', 'running', 'starting', 'stopping',  }

def check_library_run_summary_state(value: str) -> LibraryRunSummaryState:
    if value in LIBRARY_RUN_SUMMARY_STATE_VALUES:
        return cast(LibraryRunSummaryState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_RUN_SUMMARY_STATE_VALUES!r}")
