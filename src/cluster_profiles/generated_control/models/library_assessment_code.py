from typing import Literal

LibraryAssessmentCode = Literal['library.assessment_unavailable', 'library.cache_missing', 'library.capacity_unavailable', 'library.insufficient_nodes']

LIBRARY_ASSESSMENT_CODE_VALUES: set[LibraryAssessmentCode] = { 'library.assessment_unavailable', 'library.cache_missing', 'library.capacity_unavailable', 'library.insufficient_nodes',  }

def check_library_assessment_code(value: str) -> LibraryAssessmentCode:
    if value in LIBRARY_ASSESSMENT_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_ASSESSMENT_CODE_VALUES!r}")
