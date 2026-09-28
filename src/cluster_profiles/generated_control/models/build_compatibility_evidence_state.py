from typing import Literal

BuildCompatibilityEvidenceState = Literal['compatible', 'incompatible', 'unknown']

BUILD_COMPATIBILITY_EVIDENCE_STATE_VALUES: set[BuildCompatibilityEvidenceState] = { 'compatible', 'incompatible', 'unknown',  }

def check_build_compatibility_evidence_state(value: str) -> BuildCompatibilityEvidenceState:
    if value in BUILD_COMPATIBILITY_EVIDENCE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {BUILD_COMPATIBILITY_EVIDENCE_STATE_VALUES!r}")
