from typing import Literal

LibraryProjectionCode = Literal['projection.evidence_truncated', 'projection.reasons_truncated']

LIBRARY_PROJECTION_CODE_VALUES: set[LibraryProjectionCode] = { 'projection.evidence_truncated', 'projection.reasons_truncated',  }

def check_library_projection_code(value: str) -> LibraryProjectionCode:
    if value in LIBRARY_PROJECTION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PROJECTION_CODE_VALUES!r}")
