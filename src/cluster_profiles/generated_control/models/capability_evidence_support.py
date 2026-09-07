from typing import Literal, cast

CapabilityEvidenceSupport = Literal['supported', 'unknown', 'unsupported']

CAPABILITY_EVIDENCE_SUPPORT_VALUES: set[CapabilityEvidenceSupport] = { 'supported', 'unknown', 'unsupported',  }

def check_capability_evidence_support(value: str) -> CapabilityEvidenceSupport:
    if value in CAPABILITY_EVIDENCE_SUPPORT_VALUES:
        return cast(CapabilityEvidenceSupport, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CAPABILITY_EVIDENCE_SUPPORT_VALUES!r}")
