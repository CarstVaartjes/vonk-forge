from typing import Literal, cast

LibraryProjectionReasonSeverity = Literal['error', 'info', 'warning']

LIBRARY_PROJECTION_REASON_SEVERITY_VALUES: set[LibraryProjectionReasonSeverity] = { 'error', 'info', 'warning',  }

def check_library_projection_reason_severity(value: str) -> LibraryProjectionReasonSeverity:
    if value in LIBRARY_PROJECTION_REASON_SEVERITY_VALUES:
        return cast(LibraryProjectionReasonSeverity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PROJECTION_REASON_SEVERITY_VALUES!r}")
