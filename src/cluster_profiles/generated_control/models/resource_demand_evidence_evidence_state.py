from typing import Literal, cast

ResourceDemandEvidenceEvidenceState = Literal['declared', 'fresh', 'measured', 'stale', 'unknown']

RESOURCE_DEMAND_EVIDENCE_EVIDENCE_STATE_VALUES: set[ResourceDemandEvidenceEvidenceState] = { 'declared', 'fresh', 'measured', 'stale', 'unknown',  }

def check_resource_demand_evidence_evidence_state(value: str) -> ResourceDemandEvidenceEvidenceState:
    if value in RESOURCE_DEMAND_EVIDENCE_EVIDENCE_STATE_VALUES:
        return cast(ResourceDemandEvidenceEvidenceState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RESOURCE_DEMAND_EVIDENCE_EVIDENCE_STATE_VALUES!r}")
