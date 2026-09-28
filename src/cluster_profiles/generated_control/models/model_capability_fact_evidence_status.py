from typing import Literal

ModelCapabilityFactEvidenceStatus = Literal['contradicted', 'declared', 'tested', 'unknown']

MODEL_CAPABILITY_FACT_EVIDENCE_STATUS_VALUES: set[ModelCapabilityFactEvidenceStatus] = { 'contradicted', 'declared', 'tested', 'unknown',  }

def check_model_capability_fact_evidence_status(value: str) -> ModelCapabilityFactEvidenceStatus:
    if value in MODEL_CAPABILITY_FACT_EVIDENCE_STATUS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_CAPABILITY_FACT_EVIDENCE_STATUS_VALUES!r}")
