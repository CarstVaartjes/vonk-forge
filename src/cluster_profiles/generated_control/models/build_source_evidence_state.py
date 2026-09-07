from typing import Literal, cast

BuildSourceEvidenceState = Literal['available', 'missing', 'unknown']

BUILD_SOURCE_EVIDENCE_STATE_VALUES: set[BuildSourceEvidenceState] = { 'available', 'missing', 'unknown',  }

def check_build_source_evidence_state(value: str) -> BuildSourceEvidenceState:
    if value in BUILD_SOURCE_EVIDENCE_STATE_VALUES:
        return cast(BuildSourceEvidenceState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {BUILD_SOURCE_EVIDENCE_STATE_VALUES!r}")
