from typing import Literal, cast

LibraryRunSummaryRouteState = Literal['failed', 'pending', 'published', 'withdrawn']

LIBRARY_RUN_SUMMARY_ROUTE_STATE_VALUES: set[LibraryRunSummaryRouteState] = { 'failed', 'pending', 'published', 'withdrawn',  }

def check_library_run_summary_route_state(value: str) -> LibraryRunSummaryRouteState:
    if value in LIBRARY_RUN_SUMMARY_ROUTE_STATE_VALUES:
        return cast(LibraryRunSummaryRouteState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_RUN_SUMMARY_ROUTE_STATE_VALUES!r}")
