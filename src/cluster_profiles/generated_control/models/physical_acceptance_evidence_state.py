from typing import Literal, cast

PhysicalAcceptanceEvidenceState = Literal['accepted', 'failed', 'identity_mismatch', 'not_qualified']

PHYSICAL_ACCEPTANCE_EVIDENCE_STATE_VALUES: set[PhysicalAcceptanceEvidenceState] = { 'accepted', 'failed', 'identity_mismatch', 'not_qualified',  }

def check_physical_acceptance_evidence_state(value: str) -> PhysicalAcceptanceEvidenceState:
    if value in PHYSICAL_ACCEPTANCE_EVIDENCE_STATE_VALUES:
        return cast(PhysicalAcceptanceEvidenceState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PHYSICAL_ACCEPTANCE_EVIDENCE_STATE_VALUES!r}")
