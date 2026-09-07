from typing import Literal, cast

FreshnessEvidenceState = Literal['fresh', 'stale', 'unknown']

FRESHNESS_EVIDENCE_STATE_VALUES: set[FreshnessEvidenceState] = { 'fresh', 'stale', 'unknown',  }

def check_freshness_evidence_state(value: str) -> FreshnessEvidenceState:
    if value in FRESHNESS_EVIDENCE_STATE_VALUES:
        return cast(FreshnessEvidenceState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FRESHNESS_EVIDENCE_STATE_VALUES!r}")
