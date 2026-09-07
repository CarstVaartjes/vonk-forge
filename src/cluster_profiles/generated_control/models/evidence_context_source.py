from typing import Literal, cast

EvidenceContextSource = Literal['agent', 'controller']

EVIDENCE_CONTEXT_SOURCE_VALUES: set[EvidenceContextSource] = { 'agent', 'controller',  }

def check_evidence_context_source(value: str) -> EvidenceContextSource:
    if value in EVIDENCE_CONTEXT_SOURCE_VALUES:
        return cast(EvidenceContextSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {EVIDENCE_CONTEXT_SOURCE_VALUES!r}")
