from typing import Literal, cast

CapabilityEvidenceEvidence = Literal['not-tested', 'observed', 'tested', 'unknown']

CAPABILITY_EVIDENCE_EVIDENCE_VALUES: set[CapabilityEvidenceEvidence] = { 'not-tested', 'observed', 'tested', 'unknown',  }

def check_capability_evidence_evidence(value: str) -> CapabilityEvidenceEvidence:
    if value in CAPABILITY_EVIDENCE_EVIDENCE_VALUES:
        return cast(CapabilityEvidenceEvidence, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CAPABILITY_EVIDENCE_EVIDENCE_VALUES!r}")
