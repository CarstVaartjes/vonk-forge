from typing import Literal, cast

LibraryCapabilityFactEvidenceStatus = Literal['contradicted', 'declared', 'tested', 'unknown']

LIBRARY_CAPABILITY_FACT_EVIDENCE_STATUS_VALUES: set[LibraryCapabilityFactEvidenceStatus] = { 'contradicted', 'declared', 'tested', 'unknown',  }

def check_library_capability_fact_evidence_status(value: str) -> LibraryCapabilityFactEvidenceStatus:
    if value in LIBRARY_CAPABILITY_FACT_EVIDENCE_STATUS_VALUES:
        return cast(LibraryCapabilityFactEvidenceStatus, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_CAPABILITY_FACT_EVIDENCE_STATUS_VALUES!r}")
