from typing import Literal, cast

LibraryPlacementReasonSeverity = Literal['error', 'info', 'warning']

LIBRARY_PLACEMENT_REASON_SEVERITY_VALUES: set[LibraryPlacementReasonSeverity] = { 'error', 'info', 'warning',  }

def check_library_placement_reason_severity(value: str) -> LibraryPlacementReasonSeverity:
    if value in LIBRARY_PLACEMENT_REASON_SEVERITY_VALUES:
        return cast(LibraryPlacementReasonSeverity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_REASON_SEVERITY_VALUES!r}")
