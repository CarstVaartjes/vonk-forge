from typing import Literal

RunSwitchBuildEvidenceState = Literal['available', 'building', 'failed', 'incompatible', 'missing', 'planned', 'unknown']

RUN_SWITCH_BUILD_EVIDENCE_STATE_VALUES: set[RunSwitchBuildEvidenceState] = { 'available', 'building', 'failed', 'incompatible', 'missing', 'planned', 'unknown',  }

def check_run_switch_build_evidence_state(value: str) -> RunSwitchBuildEvidenceState:
    if value in RUN_SWITCH_BUILD_EVIDENCE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_BUILD_EVIDENCE_STATE_VALUES!r}")
