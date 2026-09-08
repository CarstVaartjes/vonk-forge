from typing import Literal, cast

EvidenceAgeFreshness = Literal['current', 'stale', 'unknown']

EVIDENCE_AGE_FRESHNESS_VALUES: set[EvidenceAgeFreshness] = { 'current', 'stale', 'unknown',  }

def check_evidence_age_freshness(value: str) -> EvidenceAgeFreshness:
    if value in EVIDENCE_AGE_FRESHNESS_VALUES:
        return cast(EvidenceAgeFreshness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {EVIDENCE_AGE_FRESHNESS_VALUES!r}")
